"""The AST validator: table-driven. Every row is code and the rule that must catch it (or None when it must pass)."""

import pytest

from rectifi.charts.validate import MAX_BYTES, extract_code, validate

HEAD = "import matplotlib.pyplot as plt\nimport pandas as pd\nfig, ax = plt.subplots(figsize=(10, 6))\n"

CASES = [
    # (id, code, rule that must fire or None, request)
    ("plain bar chart", HEAD + "d = df.groupby('category').amount.sum()\nax.bar(d.index, d.values)\nax.set_title('Spend by category')\n", None, ""),
    ("fig from gcf", "import matplotlib.pyplot as plt\nplt.plot([1, 2])\nfig = plt.gcf()\n", None, ""),
    ("seaborn and ticker", "import seaborn as sns\nimport matplotlib.ticker as mt\nimport matplotlib.dates as md\nfig, ax = plt.subplots()\n".replace("fig, ax", "import matplotlib.pyplot as plt\nfig, ax"), None, ""),
    ("from-import of names", "from matplotlib import pyplot as plt\nfrom collections import Counter\nfig, ax = plt.subplots()\n", None, ""),
    ("fig inside a with", "import matplotlib.pyplot as plt\nwith plt.rc_context({}):\n    fig, ax = plt.subplots()\n", None, ""),
    ("fig inside an if", "import matplotlib.pyplot as plt\nif len(df) > 3:\n    fig, ax = plt.subplots()\nelse:\n    fig, ax = plt.subplots()\n", None, ""),
    ("figsize tuple allowed", HEAD.replace("(10, 6)", "(12, 7)"), None, ""),
    ("dpi and fontsize layout values", HEAD + "ax.set_title('t', fontsize=14)\nplt.xticks(rotation=45)\nax.text(0, 0, 'x', size=12)\nax.legend(fontsize=9)\n", None, ""),
    ("layout keyword may be up to 1000", HEAD.replace("figsize=(10, 6)", "figsize=(10, 6), dpi=300") + "ax.hist(df.amount, bins=500)\n", None, ""),
    ("year 2026 allowed", HEAD + "x = df[df.date.dt.year == 2026]\n", None, ""),
    ("ISO date strings allowed", HEAD + "x = df[df.date >= '2026-09-01']\ny = df[df.month == '2026-10']\n", None, ""),
    ("year inside a longer text", HEAD + "ax.set_title('Spending, Jan to Mar 2026')\n", None, ""),
    ("hex colour allowed", HEAD + "ax.bar([1, 2], [3, 4], color='#1F4E5F')\nax.set_facecolor('#123456')\n", None, ""),
    ("a number the user typed is allowed", HEAD + "x = df[df.amount > 5000]\n", None, "show payments above 5000 rupees"),
    ("a typed number with k suffix", HEAD + "x = df[df.amount > 2500]\n", None, "entries over 2.5k"),
    ("money through inr()", HEAD + "ax.set_title(f'Total {inr(df.amount_paise.sum())}')\n", None, ""),
    ("percent maths", HEAD + "x = df.amount / df.amount.sum() * 100\n", None, ""),
    ("type with one argument", HEAD + "t = type(df)\n", None, ""),
    ("underscore throwaway name", HEAD + "for _ in range(3):\n    pass\n", None, ""),
    # imports
    ("import os", "import os\nfig = 1\n", "import", ""),
    ("import subprocess", "import subprocess\nfig = 1\n", "import", ""),
    ("import sys as alias", "import sys as s\nfig = 1\n", "import", ""),
    ("from os import system", "from os import system\nfig = 1\n", "import", ""),
    ("relative import", "from . import x\nfig = 1\n", "import", ""),
    ("star import", "from numpy import *\nfig = 1\n", "import", ""),
    ("import a submodule not on the list", "import matplotlib.cbook\nfig = 1\n", "import", ""),
    ("import plotly without the flag", "import plotly.express as px\nfig = px.bar()\n", "import", ""),
    ("from-import of a forbidden name", "from pandas import read_csv\nfig = 1\n", "import", ""),
    ("from-import of an underscore name", "from matplotlib import _api\nfig = 1\n", "import", ""),
    ("import importlib", "import importlib\nfig = 1\n", "import", ""),
    # names
    ("open()", "fig = open('x')\n", "forbidden_name", ""),
    ("eval()", "fig = eval('1')\n", "forbidden_name", ""),
    ("exec()", "exec('x = 1')\nfig = 1\n", "forbidden_name", ""),
    ("compile()", "compile('1', 'x', 'eval')\nfig = 1\n", "forbidden_name", ""),
    ("__import__", "m = __import__('os')\nfig = 1\n", "forbidden_name", ""),
    ("getattr", "x = getattr(df, 'amount')\nfig = 1\n", "forbidden_name", ""),
    ("setattr", "setattr(df, 'a', 1)\nfig = 1\n", "forbidden_name", ""),
    ("globals()", "g = globals()\nfig = 1\n", "forbidden_name", ""),
    ("vars()", "v = vars(df)\nfig = 1\n", "forbidden_name", ""),
    ("input()", "x = input()\nfig = 1\n", "forbidden_name", ""),
    ("breakpoint()", "breakpoint()\nfig = 1\n", "forbidden_name", ""),
    ("type with three arguments", "T = type('T', (), {})\nfig = 1\n", "forbidden_name", ""),
    ("a dunder name", "x = __builtins__\nfig = 1\n", "forbidden_name", ""),
    ("unicode lookalike for eval", "fig = ｅｖａｌ('1')\n", "forbidden_name", ""),
    # attributes
    ("dunder attribute", "x = df.__class__.__mro__\nfig = 1\n", "forbidden_attr", ""),
    ("single underscore attribute", "x = df._data\nfig = 1\n", "forbidden_attr", ""),
    ("to_csv", HEAD + "df.to_csv('x.csv')\n", "forbidden_attr", ""),
    ("to_pickle", HEAD + "df.to_pickle('x')\n", "forbidden_attr", ""),
    ("read_csv", "import pandas as pd\nx = pd.read_csv('a')\nfig = 1\n", "forbidden_attr", ""),
    ("read_sql", "import pandas as pd\nx = pd.read_sql('select 1', None)\nfig = 1\n", "forbidden_attr", ""),
    ("savefig", HEAD + "fig.savefig('x.png')\n", "forbidden_attr", ""),
    ("show", HEAD + "plt.show()\n", "forbidden_attr", ""),
    ("query", HEAD + "x = df.query('amount > 3')\n", "forbidden_attr", ""),
    ("df.eval", HEAD + "x = df.eval('amount * 2')\n", "forbidden_attr", ""),
    ("numpy load", "import numpy as np\nx = np.load('a.npy')\nfig = 1\n", "forbidden_attr", ""),
    ("numpy loadtxt", "import numpy as np\nx = np.loadtxt('a')\nfig = 1\n", "forbidden_attr", ""),
    ("numpy memmap", "import numpy as np\nx = np.memmap('a')\nfig = 1\n", "forbidden_attr", ""),
    ("matplotlib.use", "import matplotlib\nmatplotlib.use('pdf')\nfig = 1\n", "forbidden_attr", ""),
    ("os.environ through a module", "import matplotlib\nx = matplotlib.os.environ\nfig = 1\n", "forbidden_attr", ""),
    ("system()", "import matplotlib\nmatplotlib.os.system('ls')\nfig = 1\n", "forbidden_attr", ""),
    ("seaborn load_dataset", "import seaborn as sns\nx = sns.load_dataset('tips')\nfig = 1\n", "forbidden_attr", ""),
    ("imread", "import matplotlib.pyplot as plt\nx = plt.imread('a.png')\nfig = 1\n", "forbidden_attr", ""),
    ("format-string attribute escape", "x = '{0.__class__}'.format(df)\nfig = 1\n", "dunder_string", ""),
    # syntax kinds
    ("class definition", "class A:\n    pass\nfig = 1\n", "syntax_kind", ""),
    ("async def", "async def f():\n    pass\nfig = 1\n", "syntax_kind", ""),
    # money literals: strings
    ("rupee string", HEAD + "ax.set_title('You spent ₹5,000')\n", "money_string", ""),
    ("Rs. string", HEAD + "ax.set_title('Rs. 250 on food')\n", "money_string", ""),
    ("INR string", HEAD + "ax.set_title('INR 250 on food')\n", "money_string", ""),
    ("grouped number string", HEAD + "ax.set_title('Spent 12,500 this month')\n", "money_string", ""),
    ("Indian grouping string", HEAD + "ax.set_title('Spent 1,25,000')\n", "money_string", ""),
    ("four digit string", HEAD + "ax.set_title('Spent 5000 this month')\n", "money_string", ""),
    ("rupee string in an f-string literal part", HEAD + "ax.set_title(f'Total ₹4200 and {inr(1)}')\n", "money_string", ""),
    # money literals: numbers
    ("5000 literal", HEAD + "ax.axhline(5000)\n", "number", ""),
    ("101 literal", HEAD + "x = 101\n", "number", ""),
    ("negative big literal", HEAD + "x = -5000\n", "number", ""),
    ("float big literal", HEAD + "x = 1250.5\n", "number", ""),
    ("1e4 literal", HEAD + "x = 1e4\n", "number", ""),
    ("constant folding 50 * 100", HEAD + "x = 50 * 100\n", "number", ""),
    ("constant folding 10 ** 4", HEAD + "x = 10 ** 4\n", "number", ""),
    ("huge power", HEAD + "x = 9 ** 9 ** 9\n", "number", ""),
    ("big number in a call", HEAD + "ax.text(0, 5000, 'x')\n", "number", ""),
    ("layout keyword over 1000", HEAD + "ax.set_title('t', fontsize=5000)\n", "number", ""),
    ("number not typed by the user", HEAD + "x = 7500\n", "number", "show food spending"),
    # contract
    ("no fig", "import matplotlib.pyplot as plt\nax = plt.subplots()\n", "contract", ""),
    ("fig only inside a function", "import matplotlib.pyplot as plt\ndef f():\n    fig = plt.figure()\n    return fig\n", "contract", ""),
    # size and syntax
    ("empty", "", "empty", ""),
    ("syntax error", "def (:\n", "syntax", ""),
    ("too big", "fig = 1\n" + "# " + "x" * MAX_BYTES + "\n", "size", ""),
    ("too many lines", "fig = 1\n" + "\n" * 250, "size", ""),
    ("too many nodes", "fig = [" + ", ".join(["a"] * 1600) + "]\n", "size", ""),
    ("null byte", "fig = 1\x00\n", "syntax", ""),
]


@pytest.mark.parametrize("name,code,rule,typed", CASES, ids=[c[0] for c in CASES])
def test_validator_table(name, code, rule, typed):
    result = validate(code, request=typed, allow_plotly=False)
    if rule is None:
        assert result.ok, result.feedback()
    else:
        assert not result.ok, f"{name} was accepted"
        assert rule in result.rules, (rule, result.rules, result.feedback())


def test_the_number_message_tells_the_model_what_to_do():
    result = validate(HEAD + "x = 5000\n")
    assert "Compute them from df" in result.feedback() and "line 4" in result.feedback()


def test_plotly_is_allowed_only_behind_the_flag(monkeypatch):
    code = "import plotly.express as px\nfig = px.bar()\n"
    assert not validate(code).ok
    monkeypatch.setenv("RECTIFI_CHARTS_PLOTLY", "1")
    assert validate(code).ok
    assert validate(code, allow_plotly=False).ok is False


def test_all_violations_are_reported_not_just_the_first():
    result = validate("import os\nx = open('a')\ny = 5000\nfig = 1\n")
    assert {"import", "forbidden_name", "number"} <= set(result.rules)


def test_extract_code_takes_the_python_fence():
    assert extract_code("Here you go:\n```python\nfig = 1\n```\nbye") == "fig = 1\n"
    assert extract_code("```\nfig = 2\n```") == "fig = 2\n"
    assert extract_code("fig = 3") == "fig = 3\n"
    assert extract_code("```python\nfig = 4\n") == "fig = 4\n"          # the reply was cut off before the closing fence
