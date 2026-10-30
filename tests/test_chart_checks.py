"""The figure checks: a made-up figure fails, a computed one passes, whole-rupee truncation passes, a missing title fails."""

from decimal import Decimal

import pytest

from rectifi.charts import checks
from rectifi.charts.checks import figures_in, run_checks


def axis(title="Food spend", xlabel="Month", ylabel="Rupees", values=(1250.5, 500.0, 100.25), texts=(), legend=(), kind="bar", is_pie=False):
    if kind == "wedge":                        # the child reports a pie as a count of wedges, with no values
        return {**axis(title, xlabel, ylabel, (), texts, legend), "is_pie": True,
                "artists": [{"type": "wedge", "n": 4, "values": [], "values_x": [], "tops": [], "rights": []}]}
    return {"title": title, "title_left": "", "title_right": "", "xlabel": xlabel, "ylabel": ylabel, "texts": list(texts), "legend": list(legend),
            "xticks": [], "yticks": [], "is_pie": is_pie, "visible": True,
            "artists": [{"type": kind, "n": len(values), "values": list(values), "values_x": [], "tops": list(values), "rights": []}]}


def info(*axes, suptitle=""):
    return {"suptitle": suptitle, "n_axes": len(axes), "axes": list(axes), "figsize": [8, 5]}


REF = {"spending": {"overall": Decimal("1850.75"), "by_category": {"Food": Decimal("1250.50")}, "by_month": {}, "by_category_month": {}, "by_day": {}},
       "income": {"overall": Decimal("90000.00")}, "entries": 12, "budget_limits": {"by_month": {"2026-10": Decimal("5000.00")}}}
PNG = {"light": b"x" * 9000, "dark": b"x" * 9000}


def result(i, **kw):
    return run_checks(i, png=kw.pop("png", PNG), reference=kw.pop("reference", REF), **kw)


def failing(r):
    return [c["name"] for c in r["checks"] if not c["ok"]]


def test_a_good_chart_passes_every_check():
    r = result(info(axis(title="You spent ₹1,850.75 in total")))
    assert r["ok"], r["failures"]
    assert {c["name"] for c in r["checks"]} >= {"has_data", "labelled", "figures_match", "image_size", "glyphs"}


def test_a_title_with_a_figure_that_is_not_in_the_data_fails():
    r = result(info(axis(title="You spent ₹4,200 on food")))
    assert failing(r) == ["figures_match"]
    assert "₹4,200" in r["failures"][0] and "isn't in your data" in r["failures"][0]


def test_a_sum_of_the_plotted_values_passes():
    assert result(info(axis(values=(1000.10, 250.40), title="Total ₹1,250.50", texts=["₹1,000.10"])))["ok"]


def test_a_total_from_the_reference_numbers_passes():
    assert result(info(axis(values=(1.0, 2.0), title="Budget ₹5,000.00 and spend ₹1,850.75")))["ok"]


def test_a_rupee_figure_truncated_or_rounded_to_whole_rupees_passes():
    assert result(info(axis(title="₹1,850 spent")))["ok"]          # 1850.75 truncated, as fmt_inr(paise=False) does
    assert result(info(axis(title="₹1,851 spent")))["ok"]          # rounded
    assert not result(info(axis(title="₹1,852 spent")))["ok"]


def test_a_chart_that_plots_paise_may_quote_rupees():
    assert result(info(axis(values=(125050, 50000), title="Food ₹1,250.50")))["ok"]


def test_an_abbreviation_cannot_be_checked_and_fails():
    r = result(info(axis(title="Spent ₹1.8k")))
    assert failing(r) == ["figures_match"] and "inr()" in r["failures"][0]


def test_a_grouped_number_without_the_rupee_sign_is_also_checked():
    assert failing(result(info(axis(title="Spent 4,200 so far")))) == ["figures_match"]
    assert result(info(axis(title="1,850.75 spent")))["ok"]


def test_counts_and_small_numbers_are_not_claims():
    assert result(info(axis(title="Top 5 payees, 214 entries, 2026")), entries=214)["ok"]


def test_legend_annotations_and_suptitle_are_checked_but_tick_labels_are_not():
    base = axis()
    assert failing(result(info(base, suptitle="₹9,999 in total"))) == ["figures_match"]
    assert failing(result(info(axis(texts=["₹7,777"])))) == ["figures_match"]
    assert failing(result(info(axis(legend=["Spent ₹3,333"])))) == ["figures_match"]
    ticks = axis()
    ticks["xticks"] = ["₹8,888"]
    assert result(info(ticks))["ok"]


def test_a_missing_title_fails():
    r = result(info(axis(title="")))
    assert failing(r) == ["labelled"] and "no title" in r["failures"][0]


def test_missing_axis_labels_fail_unless_it_is_a_pie():
    assert failing(result(info(axis(xlabel=""))) ) == ["labelled"]
    assert result(info(axis(xlabel="", ylabel="", kind="wedge", values=(), is_pie=True)), request="pie of last month")["ok"]


def test_a_pie_nobody_asked_for_fails():
    r = result(info(axis(xlabel="", ylabel="", kind="wedge", values=(), is_pie=True)), request="spending by category")
    assert failing(r) == ["no_unasked_pie"]


def test_empty_nan_only_or_single_point_charts_fail():
    assert failing(result(info(axis(values=()))))[0] == "has_data"
    assert "has_data" in failing(result(info()))
    assert failing(result(info(axis(values=(5.0,))), entries=20)) == ["enough_points"]
    assert result(info(axis(values=(5.0,))), entries=1)["ok"]


def test_a_missing_rupee_glyph_and_a_tiny_image_fail():
    assert failing(result(info(axis()), warnings=["Glyph 8377 (\\N{INDIAN RUPEE SIGN}) missing from font(s) DejaVu Sans."])) == ["glyphs"]
    assert failing(result(info(axis()), png={"light": b"x" * 100, "dark": b"x" * 9000})) == ["image_size"]


def test_figures_in_reads_indian_grouping_and_decimals():
    assert figures_in("₹1,25,000.50 and Rs. 250 and INR 5") == [("₹1,25,000.50", Decimal("125000.50")), ("Rs. 250", Decimal("250.00")), ("INR 5", Decimal("5.00"))]
    assert figures_in("₹2L")[0][1] is None


def test_the_alt_text_comes_from_the_checks():
    r = result(info(axis()))
    text = checks.alt_text(info(axis()), r)
    assert "Food spend" in text and "bar chart" in text and "figures match" in text
