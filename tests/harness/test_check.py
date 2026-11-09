from episteme.harness.check import FIELD_TERMS, check_answer, route_claim
from episteme.reference.claims import Claim, Coverage, LookupResult, Provenance


def _res(
    text="Trial NCT01234567 started 2020-03-04",
    value="K1",
    key="EMEA/H/C/PSUSA/00001/202001",
):
    claim = Claim(text, value, "fetched", Provenance("s", None, None, key, None), 1.0, "d")
    return [LookupResult((claim,), Coverage(1))]


def test_check_flags_an_invented_date():
    assert check_answer("It began on 2021-05-06.", _res()) == ["2021-05-06"]


def test_check_flags_an_invented_psusa_number():
    bad = check_answer("See EMEA/H/C/PSUSA/00009/202101.", _res())
    assert bad == ["EMEA/H/C/PSUSA/00009/202101"]


def test_check_passes_values_present_in_results():
    ans = "Trial NCT01234567 started 2020-03-04 (EMEA/H/C/PSUSA/00001/202001)."
    assert check_answer(ans, _res()) == []


def test_check_does_not_flag_digits_inside_identifier_or_small_integers():
    res = _res(text="Trial NCT01234567")
    assert check_answer("NCT01234567 had 5 arms.", res) == []


def test_check_flags_other_identifiers_and_counts():
    ans = (
        "NCT99999999, PMID 123456, UNII 1A2B3C4D5E, NDC 12345-678-90,"
        " 25 January 2020, 1,234 reports, 57 cases"
    )
    bad = check_answer(ans, _res())
    wanted = ("NCT99999999", "123456", "1A2B3C4D5E", "12345-678-90", "25 January 2020")
    for v in (*wanted, "1,234", "57"):
        assert any(v in b for b in bad), v


def test_check_count_present_in_results():
    res = _res(text="1,234 reports in 2020Q1")
    assert check_answer("There were 1234 reports.", res) == []


def _resolve_for(*names):
    def resolve(text):
        if text.lower() in names:
            prov = Provenance("s", None, None, "7", None)
            return LookupResult((Claim("m", "7", "mapped", prov, 1.0, "d"),), Coverage(1))
        return LookupResult((), Coverage(0))

    return resolve


def test_field_terms_cover_lookups():
    wanted = {"approval_status", "safety_actions", "current_label", "trials", "faers_counts"}
    assert set(FIELD_TERMS) >= wanted


def test_claim_with_entity_and_field_routes_to_lookup():
    got = route_claim("Is metformin approved in the EU?", _resolve_for("metformin"))
    assert got == ("lookup", "approval_status")


def test_mechanism_claim_stays_with_model():
    got = route_claim("Metformin lowers hepatic glucose output", _resolve_for("metformin"))
    assert got == ("model", None)


def test_field_without_resolvable_entity_stays_with_model():
    assert route_claim("Is zzzzdrug approved?", _resolve_for("metformin")) == ("model", None)
