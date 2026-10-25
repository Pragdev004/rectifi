"""scripts/devtools/models.py: the doctor round trip (standard-library client), the RAM tier, ollama-setup and the Modelfiles."""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
from test_llm_probe import FakeCompat

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def models():
    spec = importlib.util.spec_from_file_location("rectifi_devtool_models_test", ROOT / "scripts" / "devtools" / "models.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def settings(**values):
    return lambda name, default="": values.get(name, default)


@pytest.fixture(autouse=True)
def quiet_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def by_text(checks, fragment):
    return [c for c in checks if fragment in c[1]]


# ───────────── the module is standard library only ─────────────

def test_models_py_imports_only_the_standard_library():
    text = (ROOT / "scripts" / "devtools" / "models.py").read_text(encoding="utf-8")
    import ast

    imports = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            imports |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    third_party = imports - set(sys.stdlib_module_names) - {"__future__"}
    assert third_party == set(), third_party
    assert "shell=True" not in text and "rectifi" not in imports


# ───────────── tiers ─────────────

@pytest.mark.parametrize("ram, gpu, tier", [
    (4, False, "small"), (7.7, False, "small"), (15.6, False, "laptop"), (11.7, False, "laptop"), (23.4, False, "large"),
    (64, False, "large"), (8, True, "large"), (None, False, "laptop"),
])
def test_pick_tier(models, ram, gpu, tier):
    assert models.pick_tier(ram, gpu) == tier


def test_the_tier_table_is_the_plan(models):
    t = models.TIERS
    assert t["laptop"]["chat"] == "qwen2.5:7b" and t["laptop"]["code"] == "qwen2.5-coder:7b" and t["laptop"]["classify"] == "qwen2.5:3b"
    assert t["small"]["chat"] == "llama3.2:3b" and t["small"]["code"] == "qwen2.5-coder:3b"
    assert t["large"]["chat"] == "qwen3:14b" and t["large"]["code"] == "qwen2.5-coder:14b" and t["large"]["classify"] == "qwen2.5:7b"
    assert t["oracle"]["chat"] == "qwen2.5:3b"
    assert all(v["embed"] == "nomic-embed-text" for v in t.values())


def test_ram_is_read_on_this_machine(models):
    ram = models.read_ram_gb()
    assert ram is None or ram > 0.5


def test_env_lines_use_the_modelfile_models(models):
    lines = models.env_lines("laptop")
    assert "RECTIFI_MODEL_CHAT=qwen2.5:7b" in lines and "RECTIFI_MODEL_CLASSIFY=rectifi-classify" in lines
    assert "RECTIFI_MODEL_CODE=rectifi-chartcoder" in lines and "RECTIFI_EMBED_MODEL=nomic-embed-text" in lines


# ───────────── Modelfiles ─────────────

@pytest.mark.parametrize("name, base", [("Modelfile.classify", "qwen2.5:3b"), ("Modelfile.chartcoder", "qwen2.5-coder:7b")])
def test_the_modelfiles_parse(models, name, base):
    parsed = models.parse_modelfile((ROOT / "deploy" / name).read_text(encoding="utf-8"))
    assert parsed["from"] == base and parsed["system"]
    assert parsed["parameters"] == {"temperature": "0", "num_ctx": "8192"}


def test_modelfile_parser_rejects_nonsense(models):
    with pytest.raises(ValueError):
        models.parse_modelfile("FROM x\nSYSTEM hi\nBOGUS 1")
    with pytest.raises(ValueError):
        models.parse_modelfile("SYSTEM hi")
    with pytest.raises(ValueError):
        models.parse_modelfile("FROM x\nSYSTEM hi\nPARAMETER temperature")
    assert models.parse_modelfile("FROM x\nSYSTEM one line\n# note")["system"] == "one line"


def test_render_modelfile_swaps_only_from(models):
    text = (ROOT / "deploy" / "Modelfile.classify").read_text(encoding="utf-8")
    out = models.render_modelfile(text, "qwen2.5:7b")
    assert models.parse_modelfile(out)["from"] == "qwen2.5:7b"
    assert models.parse_modelfile(out)["system"] == models.parse_modelfile(text)["system"]


# ───────────── ollama-setup ─────────────

def test_ollama_setup_dry_run_does_nothing(models, monkeypatch, capsys):
    monkeypatch.setattr(models, "run", lambda cmd: pytest.fail("dry run must not run commands"))
    models.ollama_setup(["--tier", "laptop", "--dry-run"])
    out = capsys.readouterr().out
    assert "qwen2.5:7b" in out and "rectifi-classify (from qwen2.5:3b)" in out and "RECTIFI_MODEL_CODE=rectifi-chartcoder" in out


def test_ollama_setup_pulls_then_creates_with_list_arguments(models, monkeypatch, capsys):
    ran, files = [], {}

    def fake_run(cmd):
        ran.append(cmd)
        if cmd[:2] == ["ollama", "create"]:
            files[cmd[2]] = Path(cmd[cmd.index("-f") + 1]).read_text(encoding="utf-8")
        return 0

    monkeypatch.setattr(models, "run", fake_run)
    monkeypatch.setattr(models.shutil, "which", lambda name: "/usr/bin/ollama")
    models.ollama_setup(["--tier", "large"])
    assert ran[:5] == [["ollama", "pull", m] for m in ("qwen3:14b", "qwen2.5:7b", "qwen2.5-coder:14b", "nomic-embed-text")] or \
        [c[2] for c in ran if c[1] == "pull"] == ["qwen3:14b", "qwen2.5:7b", "qwen2.5-coder:14b", "nomic-embed-text"]
    assert all(isinstance(c, list) for c in ran)
    assert [c[2] for c in ran if c[1] == "create"] == ["rectifi-classify", "rectifi-chartcoder"]
    assert models.parse_modelfile(files["rectifi-classify"])["from"] == "qwen2.5:7b"
    assert models.parse_modelfile(files["rectifi-chartcoder"])["from"] == "qwen2.5-coder:14b"
    assert "RECTIFI_MODEL_CLASSIFY=rectifi-classify" in capsys.readouterr().out


def test_ollama_setup_stops_when_a_pull_fails(models, monkeypatch):
    monkeypatch.setattr(models, "run", lambda cmd: 1)
    monkeypatch.setattr(models.shutil, "which", lambda name: "/usr/bin/ollama")
    with pytest.raises(SystemExit) as stop:
        models.ollama_setup(["--tier", "small"])
    assert "ollama pull" in str(stop.value)


def test_ollama_setup_without_the_ollama_command(models, monkeypatch):
    monkeypatch.setattr(models.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as stop:
        models.ollama_setup(["--tier", "small"])
    assert "ollama.com" in str(stop.value)


def test_ollama_setup_rejects_an_unknown_tier(models):
    with pytest.raises(SystemExit):
        models.ollama_setup(["--tier", "huge"])


# ───────────── the doctor round trip ─────────────

def test_the_round_trip_against_the_fake_ollama(models, fake_ollama):
    fake_ollama.reply_tool_call("ping", {"x": 1})
    checks = models.doctor_checks(settings(LLM_PROVIDER="ollama", OLLAMA_BASE_URL=fake_ollama.base_url), sandbox_check=False)
    levels = {c[1]: c[0] for c in checks}
    assert by_text(checks, "answered a real tool call with qwen2.5:7b")[0][0] == "ok"
    assert by_text(checks, "Embeddings: nomic-embed-text is downloaded")
    assert by_text(checks, "Machine:") and by_text(checks, "Suggested models")
    assert "warn" not in levels.values() and "fail" not in levels.values()
    body = fake_ollama.bodies("/api/chat")[0]
    assert body["stream"] is False and body["tools"][0]["function"]["name"] == "ping"


def test_the_round_trip_reports_role_models_and_loaded_models(models, fake_ollama):
    fake_ollama.loaded = ["qwen2.5:7b"]
    fake_ollama.reply_tool_call("ping", {"x": 1})
    checks = models.doctor_checks(settings(OLLAMA_BASE_URL=fake_ollama.base_url, RECTIFI_MODEL_CODE="qwen2.5-coder:7b",
                                           RECTIFI_MODEL_CLASSIFY="gone:1b"), sandbox_check=False)
    assert by_text(checks, "code model qwen2.5-coder:7b is downloaded")[0][0] == "ok"
    gone = by_text(checks, "classify model gone:1b")[0]
    assert gone[0] == "warn" and "ollama pull gone:1b" in gone[2]
    assert by_text(checks, "Ollama has in memory: qwen2.5:7b")


def test_a_model_without_tools_is_a_warning_with_a_suggestion(models, fake_ollama):
    fake_ollama.add_model("tiny:1b", ["completion"])
    checks = models.doctor_checks(settings(OLLAMA_BASE_URL=fake_ollama.base_url, OLLAMA_MODEL="tiny:1b"), sandbox_check=False)
    level, what, fix = by_text(checks, "does not support tool calling")[0]
    assert level == "warn" and "ollama pull qwen2.5:7b" in fix


def test_a_tool_call_written_as_text_is_a_warning(models, fake_ollama):
    fake_ollama.reply('{"name": "ping", "arguments": {"x": 1}}')
    checks = models.doctor_checks(settings(OLLAMA_BASE_URL=fake_ollama.base_url), sandbox_check=False)
    assert by_text(checks, "wrote the tool call as text")[0][0] == "warn"


def test_a_missing_model_is_a_pull_instruction(models, fake_ollama):
    checks = models.doctor_checks(settings(OLLAMA_BASE_URL=fake_ollama.base_url, OLLAMA_MODEL="nope:7b"), sandbox_check=False)
    assert "ollama pull nope:7b" in by_text(checks, "chat model nope:7b")[0][2]
    assert not by_text(checks, "round trip")


def test_ollama_down_is_a_warning_not_a_crash(models):
    checks = models.doctor_checks(settings(OLLAMA_BASE_URL="http://127.0.0.1:9"), sandbox_check=False)
    level, what, fix = by_text(checks, "ollama is not running")[0]
    assert level == "warn" and "Ollama app" in fix


def test_quick_makes_no_network_calls(models, fake_ollama):
    checks = models.doctor_checks(settings(OLLAMA_BASE_URL=fake_ollama.base_url), quick=True, sandbox_check=False)
    assert fake_ollama.calls == [] and by_text(checks, "--quick")


def test_a_slow_model_gets_the_wait_advice(models, fake_ollama):
    fake_ollama.delay = 0.6
    level, what, fix = models.ollama_round_trip(fake_ollama.base_url, "qwen2.5:7b", timeout=0.2)
    assert level == "warn" and "no answer within 0.2 seconds" in what and "loads" in fix


KEY = "gsk-doctor-secret-1234567890"


def compat_settings(server, **extra):
    return settings(LLM_PROVIDER="openai_compat", OPENAI_COMPAT_BASE_URL=server.url, OPENAI_COMPAT_API_KEY=KEY,
                    OPENAI_COMPAT_MODEL="tool-model", **extra)


def test_openai_style_round_trip(models):
    server = FakeCompat([("tool", "ping", {"x": 1})])
    try:
        checks = models.doctor_checks(compat_settings(server), sandbox_check=False)
    finally:
        server.close()
    assert by_text(checks, "answered a real tool call with tool-model")[0][0] == "ok"
    path, body, headers = server.requests[0]
    assert path == "/v1/chat/completions" and headers["authorization"] == f"Bearer {KEY}" and body["tools"]


def test_openai_style_aq_key_failure_never_prints_the_key(models):
    aq = "AQ.Ab8RN6Ixyz-secret_0987654321"
    server = FakeCompat([(400, [{"error": {"code": 400, "message": f"API key not valid. Please pass a valid API key. {aq}"}}])])
    try:
        checks = models.doctor_checks(settings(LLM_PROVIDER="openai_compat", OPENAI_COMPAT_BASE_URL=server.url,
                                               OPENAI_COMPAT_API_KEY=aq, OPENAI_COMPAT_MODEL="m"), sandbox_check=False)
    finally:
        server.close()
    level, what, fix = by_text(checks, "rejected the key")[0]
    assert level == "warn" and "HTTP 400" in what and "AIza" in fix
    assert aq not in json.dumps(checks) and "AQ.Ab8" not in json.dumps(checks)


@pytest.mark.parametrize("status, fragment", [(401, "Check the key"), (404, "model id may be wrong"), (429, "Rate limited")])
def test_openai_style_status_advice(models, status, fragment):
    server = FakeCompat([(status, {"error": {"message": "nope"}})])
    try:
        level, what, fix = models.openai_round_trip("openai_compat", compat_settings(server))
    finally:
        server.close()
    assert level == "warn" and f"HTTP {status}" in what and fragment in fix and KEY not in what + fix


def test_openai_style_missing_settings_and_quick(models):
    checks = models.doctor_checks(settings(LLM_PROVIDER="openrouter", OPENROUTER_API_KEY="k"), sandbox_check=False)
    assert "OPENROUTER_MODEL" in by_text(checks, "openrouter, but")[0][1] and "free-models" in by_text(checks, "openrouter, but")[0][2]
    checks = models.doctor_checks(settings(LLM_PROVIDER="openrouter", OPENROUTER_API_KEY="k", OPENROUTER_MODEL="a/b:free"), quick=True,
                                  sandbox_check=False)
    assert by_text(checks, "not tested: --quick")


def test_unknown_provider_and_hugging_face_note(models):
    assert "not known" in models.doctor_checks(settings(LLM_PROVIDER="banana"), sandbox_check=False)[0][1]
    checks = models.doctor_checks(settings(LLM_PROVIDER="huggingface", HF_TOKEN="hf_x" * 5), quick=True, sandbox_check=False)
    assert by_text(checks, "not free on a free account")


def test_langsmith_note_and_sandbox(models):
    checks = models.doctor_checks(settings(OLLAMA_BASE_URL="http://127.0.0.1:9", LANGSMITH_TRACING="true"))
    assert by_text(checks, "switches it off")[0][0] == "ok"
    checks = models.doctor_checks(settings(OLLAMA_BASE_URL="http://127.0.0.1:9", LANGSMITH_TRACING="true", RECTIFI_LANGSMITH="1"))
    assert by_text(checks, "LangSmith tracing is ON")[0][0] == "warn"
    assert by_text(checks, "Chart sandbox")


def test_redact_in_the_doctor(models):
    out = models.redact("failed for sk-abc123456789 and Bearer xyz.123 with hf_abcdefgh and AQ.Zz-1", ("plainsecret",))
    assert "sk-abc" not in out and "xyz.123" not in out and "hf_abc" not in out and "AQ.Zz" not in out
    assert "plainsecret" not in models.redact("x plainsecret y", ("plainsecret",))


# ───────────── dev.py wiring ─────────────

def test_dev_py_lists_the_commands(models):
    assert {"llm-check", "ollama-setup", "classify", "eval"} <= set(models.COMMANDS)
    import subprocess

    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "dev.py"), "--help"], capture_output=True, text=True, cwd=ROOT).stdout
    assert "llm-check" in out and "ollama-setup" in out


def test_eval_reports_not_built_yet_for_a_missing_module(models, monkeypatch, tmp_path):
    monkeypatch.setattr(models, "project_python", lambda: sys.executable)
    monkeypatch.setattr(models, "ROOT", tmp_path)                      # an empty project: rectifi.evals does not exist there
    with pytest.raises(SystemExit) as stop:
        models.run_eval(["rag"])
    assert "not built yet" in str(stop.value)
