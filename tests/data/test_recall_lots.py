"""Lots and substances out of the free text of ``drug/enforcement``. Pure tests; the
database side is in ``test_faers_signals.py``. The examples are trimmed from real records."""

import pytest

from episteme.config import (
    DEFAULT_FAERS_PAIR_DRUG_CAP,
    DEFAULT_RECALL_MAX_RANGE_LOTS,
    get_settings,
)
from episteme.data.regulatory.normalize import PLACEHOLDER_LOTS, is_real_lot, norm_lot
from episteme.data.regulatory.recall_lots import (
    extract_lots,
    find_ndc_products,
    lot_rows,
    name_grams,
    pick_names,
)


def test_extract_lots_from_lot_prefix():
    assert extract_lots("Lot #: AB123, Exp 12/25") == ["AB123"]


def test_extract_lots_several():
    assert extract_lots("Lot: C0822E, Exp 1/31/2024, F1121B, Exp 1/31/2024, G2721L") == [
        "C0822E",
        "F1121B",
        "G2721L",
    ]
    assert extract_lots("Lot 3C11 and 3C11A") == ["3C11", "3C11A"]
    assert extract_lots("Batch 21000279A, 21000280A") == ["21000279A", "21000280A"]


@pytest.mark.parametrize(
    ("text", "lots"),
    [
        ("Lots: 060116F Exp. 06/19; 020917F Exp. 02/20", ["060116F", "020917F"]),
        (
            "Lot #  17240669, exp. date Mar-26 17240876, exp. date Apr-26",
            ["17240669", "17240876"],
        ),
        ("Lot# B1414-160809, Retest Date AUG 2021", ["B1414-160809"]),
        (
            "Lots: 20170502@47 BUD: 7/31/2017; 20170502@56 BUD: 7/31/2017",
            ["20170502@47", "20170502@56"],
        ),
        (
            "Lot: 07292021:79 BUD: 10/27/2021; 09072021:33 BUD: 12/07",
            ["07292021:79", "09072021:33"],
        ),
        ("Lot: 45044 Discard by: 8/2/2020", ["45044"]),
        (
            "Lot# 10010 Best used by: 07/14/13  Lot# 10285 Best used by: 02/16/14",
            ["10010", "10285"],
        ),
        (
            "Lots: LD/120417 Discard by 6/2/2018; CALD/013118 Discard by 4/1/2018",
            ["LD/120417", "CALD/013118"],
        ),
        (
            "Lot #:   Exp:  WO00707825 5/31/2016  WO00713226    7/31/2016",
            ["WO00707825", "WO00713226"],
        ),
        (
            "Batch: a) 15196, Exp. 2/28/2022; 16605, Exp. 3/31/2023; b)15190, Exp. 2/28/2022",
            ["15196", "16605", "15190"],
        ),
        (
            "Lot # a) HAD1712B, Exp. date 03/2025 b) HAD1712C, exp. date 03/2025",
            ["HAD1712B", "HAD1712C"],
        ),
        (
            "Lot, expiry: AL-07232019@99, exp 9-23-2019; AL-07312019@99, exp 9-30-2019",
            ["AL-07232019@99", "AL-07312019@99"],
        ),
        ("Lot Number: 151900252M, Exp between 07/15/15-10/12/15", ["151900252M"]),
        (
            "Known lot codes (Exp Date): 06012016B (11/28/2016), 07082016A (12/31/2016)",
            ["06012016B", "07082016A"],
        ),
        (
            "Lot:17-275343, Exp. 10/3/2023; 17-275472, 17-275489, Exp.10/4/2023.",
            ["17-275343", "17-275472", "17-275489"],
        ),
        (
            "NDC 64380-933-05  Lot 7901007A, Exp Date Jun-22; Lot 7901144A & 7901178A, "
            "Exp Date Sep-22",
            ["7901007A", "7901144A", "7901178A"],
        ),
    ],
)
def test_real_recall_text(text, lots):
    assert extract_lots(text) == lots


def test_expiry_and_ndc_are_not_lots():
    assert extract_lots("NDC 12345-678-90, Exp 12/25") == []
    assert extract_lots("Lot AB12, NDC 12345-678-90, Exp 12/25") == ["AB12"]
    assert extract_lots("Lot 12345-678-90") == []
    assert extract_lots("Lot: 12/2025") == []
    assert extract_lots("Lot 500mg") == []


def test_a_date_where_the_lot_should_start_gives_no_lot():
    # The real lot is the three parts together. Taking "0515" would be a wrong lot.
    assert extract_lots("Lot #:4/26/18 0515 83425P, BUD 6/25/2018") == []


def test_explicit_range_expands_only_with_both_ends():
    assert extract_lots("Lots AB100 through AB103") == ["AB100", "AB101", "AB102", "AB103"]
    assert extract_lots("Lot# 13-920452-0-1 through 13-920452-0-3, EXP 21 JUN 2014") == [
        "13-920452-0-1",
        "13-920452-0-2",
        "13-920452-0-3",
    ]
    assert extract_lots("Lots 00098 thru 00101") == ["00098", "00099", "00100", "00101"]
    assert extract_lots("Lots AB100 through") == ["AB100"]
    assert extract_lots("Lots AB100 through 12/25") == ["AB100"]
    # Other prefixes, or a range of absurd size: the two ends only, nothing invented.
    assert extract_lots("Lots AB100 through CD103") == ["AB100", "CD103"]
    assert extract_lots("Lots AB100 through AB999999") == ["AB100", "AB999999"]


@pytest.mark.parametrize(
    "text",
    [
        "All lots",
        "all lots within expiry",
        "All lots distributed between 01/10 to 02/13",
        "All lot numbers within expiry.",
        "All Lots distributed 02/23/2021 through 03/10/2021.",
        "not able to identify the particular lot number received by a particular consignee",
        "Lots remaining within expiry",
        "",
    ],
)
def test_text_with_no_lot_gives_none(text):
    assert extract_lots(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "C51012 Exp 9/12/13 and C51201 Exp 9/6/13",
        "FG003141, FG003202, FG003296",
        "Rx #:   0392274",
        "CHLORTHALIDONE, Tablet, 50 mg has the following codes: Pedigree: W002988, EXP: 6/11/2014.",
        "SKUs: 999355; 909223",
        "61224080041",
    ],
)
def test_a_code_with_no_lot_keyword_is_left_out(text):
    assert extract_lots(text) == []


def test_recall_without_lot_is_counted():
    assert extract_lots("All lots") == []
    rows = lot_rows("D-1-2020", "r1", ["x"], "All lots", "openfda")
    assert rows == [("D-1-2020", "r1", ["x"], None, None, "none", "openfda")]


def test_lot_rows_hold_raw_norm_and_method():
    rows = lot_rows("D-2", "r1", ["x"], "Lots AB-100 through AB-101; Lot CD12", None)
    assert rows == [
        ("D-2", "r1", ["x"], "AB-100", "AB100", "range", None),
        ("D-2", "r1", ["x"], "AB-101", "AB101", "range", None),
        ("D-2", "r1", ["x"], "CD12", "CD12", "prefix", None),
    ]


def test_the_same_lot_twice_is_one_lot():
    assert extract_lots("Lot AB12, Lot AB-12") == ["AB12"]


@pytest.mark.parametrize(
    "value", ["UNKNOWN", "unknown", "UNK", "N/A", "n.a.", "NOT KNOWN", "None", "Unkonwn", "UNKNWON"]
)
def test_placeholders_are_not_lots(value):
    assert not is_real_lot(norm_lot(value))


def test_placeholder_list_is_in_normalised_form():
    assert all(norm_lot(p) == p for p in PLACEHOLDER_LOTS)


@pytest.mark.parametrize("value", ["AB123", "37-888833", "UNK123", "NA1"])
def test_real_lots_pass(value):
    assert is_real_lot(norm_lot(value))


def test_no_lot_is_not_a_real_lot():
    assert not is_real_lot(None)


def test_a_placeholder_after_a_lot_keyword_is_not_extracted():
    assert extract_lots("Lot: N/A1, UNKNOWN") == ["N/A1"]


def test_ndc_products_are_the_first_nine_digits():
    text = "Tablets, NDC 0591-2435-15 (carton), NDC 0591-2435-45 (blister); (NDC# 61553-043-34)"
    assert find_ndc_products(text) == ["005912435", "615530043"]


@pytest.mark.parametrize(
    "text", ["Call 1-800-555-1212", "Exp 2019-03-12", "Lot 37-888833", "NDC 12345-678", ""]
)
def test_other_numbers_are_not_ndcs(text):
    assert find_ndc_products(text) == []


def test_name_grams_are_normalised_and_keep_their_place():
    grams = list(name_grams("Metformin HCl, 500 mg"))
    assert (0, 1, "metformin") in grams
    assert (0, 2, "metformin hcl") in grams
    assert all(len(g) >= 5 for _, _, g in grams)


def test_name_grams_stop_at_five_words_and_short_names_are_ignored():
    grams = list(name_grams("aa bb cc dd ee ff"))
    assert max(n for _, n, _ in grams) == 5
    assert "aa" not in {g for _, _, g in grams}
    assert "aa bb" in {g for _, _, g in grams}


def test_the_longest_match_wins_and_covers_its_words():
    grams = list(name_grams("Metformin Hydrochloride Tablets USP"))
    known = {"metformin", "metformin hydrochloride", "hydrochloride"}
    assert pick_names(grams, known) == ["metformin hydrochloride"]


def test_several_names_in_one_description():
    grams = list(name_grams("Ibuprofen, with Calcium Gluconate 0.9%, Bag"))
    assert pick_names(grams, {"ibuprofen", "calcium gluconate"}) == [
        "ibuprofen",
        "calcium gluconate",
    ]


@pytest.mark.parametrize(
    ("text", "lots"),
    [
        ("Lot ABC123 500 mg", ["ABC123"]),
        ("Lot 12345 250 mL", ["12345"]),
        ("Lot 12345, 100 tablets", ["12345"]),
        ("Lot 12345 123456 mg", ["12345"]),
        ("Lot 12345 1-800-555-1212", ["12345"]),
        ("Lot 12345 (800) 555-1212", ["12345"]),
        ("Lot 12345, 555-1212", ["12345"]),
        ("Lot AB12 Exp 2025", ["AB12"]),
        ("Lot AB12 Exp 12.2025", ["AB12"]),
        ("Lot AB12 Exp 12.31.25", ["AB12"]),
        ("Lot AB12 Exp 20251231", ["AB12"]),
        ("Lot AB12 Exp12/25", ["AB12"]),
        ("Lot EXP123", ["EXP123"]),
        ("Lot BUD1234", ["BUD1234"]),
        ("Multiple lots 2020", []),
        ("Various lots 12345", []),
        ("Lot AB12 00536323307", ["AB12"]),
        ("Lot AB12 1223048610", ["AB12"]),
        ("Lot 2020", []),
        ("Lot 9781", []),
    ],
)
def test_probes_that_gave_false_lots(text, lots):
    assert extract_lots(text) == lots


@pytest.mark.parametrize(
    ("text", "lots"),
    [
        ("Lots 100 - 200 mg", []),
        ("Lots 12345 - 12400 mg", []),
        ("Lots AB100-AB102", ["AB100", "AB101", "AB102"]),
        ("Lots 12345-12347", ["12345", "12346", "12347"]),
        ("Lots AB100-CD105", ["AB100-CD105"]),
        ("Lots AB100-AB999999", ["AB100", "AB999999"]),
        ("Lot 37-888833", ["37-888833"]),
        ("Lot 123-456", ["123-456"]),
        ("Lot 12216-2", ["12216-2"]),
        ("Lot 1234/5678", []),
        ("Lot 12345/67890", ["12345", "67890"]),
        ("Lot AB12/CD34", ["AB12", "CD34"]),
        ("Lot LD/120417", ["LD/120417"]),
    ],
)
def test_range_and_separator_rules(text, lots):
    assert extract_lots(text) == lots


def test_the_range_limit_comes_from_the_settings(monkeypatch):
    monkeypatch.setenv("EPISTEME_RECALL_MAX_RANGE_LOTS", "3")
    get_settings.cache_clear()
    assert extract_lots("Lots AB100 through AB105") == ["AB100", "AB105"]
    assert extract_lots("Lots AB100 through AB102") == ["AB100", "AB101", "AB102"]
    monkeypatch.delenv("EPISTEME_RECALL_MAX_RANGE_LOTS")
    get_settings.cache_clear()
    assert get_settings().recall_max_range_lots == DEFAULT_RECALL_MAX_RANGE_LOTS == 1000
    get_settings.cache_clear()


def test_reading_stops_at_the_maker():
    text = "Gamma Tablets, Distributed by Sterile Water Labs, Silver Spring MD"
    known = {"gamma", "water", "sterile water", "silver"}
    assert pick_names(name_grams(text), known) == ["gamma"]
    text = "Made in Silver Spring, Gamma"
    assert pick_names(name_grams(text), known) == []
    text = "Gamma, MFG BY: Silver Labs"
    assert pick_names(name_grams(text), known) == ["gamma"]
    text = "Gamma, Dist. by Silver Labs"
    assert pick_names(name_grams(text), known) == ["gamma"]


def test_a_vehicle_stays_only_when_it_is_the_only_name():
    known = {"sodium chloride", "dextrose", "norepinephrine"}
    assert pick_names(name_grams("0.9% Sodium Chloride Irrigation"), known) == ["sodium chloride"]
    assert pick_names(name_grams("Norepinephrine 8 mg in 5% Dextrose, 250 mL"), known) == [
        "norepinephrine"
    ]
    assert pick_names(name_grams("Sodium Chloride in Dextrose"), known) == [
        "sodium chloride",
        "dextrose",
    ]


def test_a_slash_joins_two_names():
    grams = list(name_grams("Estradiol/Testosterone Cypionate 180 mg/mL"))
    known = {"estradiol", "testosterone cypionate"}
    assert pick_names(grams, known) == ["estradiol", "testosterone cypionate"]


def test_no_fuzzy_match():
    grams = list(name_grams("Ibuprofin Tablets"))
    assert pick_names(grams, {"ibuprofen"}) == []


def test_pair_cap_default(monkeypatch):
    monkeypatch.delenv("EPISTEME_FAERS_PAIR_DRUG_CAP", raising=False)
    get_settings.cache_clear()
    assert get_settings().faers_pair_drug_cap == DEFAULT_FAERS_PAIR_DRUG_CAP == 30
    monkeypatch.setenv("EPISTEME_FAERS_PAIR_DRUG_CAP", "12")
    get_settings.cache_clear()
    assert get_settings().faers_pair_drug_cap == 12
    get_settings.cache_clear()


def test_an_ndc_with_no_hyphens_needs_its_keyword():
    assert find_ndc_products("Tablet, NDC 00536323307.") == ["005363233"]
    assert find_ndc_products("61224080041") == []


def test_glued_and_typed_dates_are_not_lots():
    assert extract_lots("Lot #:25JAN029, exp. date 07Nov2025; 25JAN042, exp. date 14Nov2025") == [
        "25JAN029",
        "25JAN042",
    ]
    assert extract_lots("Batch Numbers: BS704, EXP 12/018;  BS725, EXP 6/2019") == [
        "BS704",
        "BS725",
    ]


def test_a_pack_size_is_not_a_lot():
    text = "Count, lots, expiry: [90-count bottle] Lot 4DU3E016, exp 01/31/2021"
    assert extract_lots(text) == ["4DU3E016"]


def test_a_lot_ending_in_a_unit_letter_is_still_a_lot():
    assert extract_lots("Lots: 020917L Exp. 02/20") == ["020917L"]
