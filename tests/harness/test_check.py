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
    wanted = ["NCT99999999", "123456", "1A2B3C4D5E", "12345-678-90", "25 January 2020"]
    assert bad == [*wanted, "1,234", "57"]


def test_long_form_date_matches_iso_date_in_results():
    assert check_answer("Start was 25 January 2020.", _res(text="date: 2020-01-25")) == []
    other = check_answer("Start was 26 January 2020.", _res(text="date: 2020-01-25"))
    assert other == ["26 January 2020"]


def test_count_is_not_matched_inside_a_date():
    res = _res(text="date: 2021-03-15", key="faers:7:2020-01-01")
    assert check_answer("There were 15 reports.", res) == ["15"]
    assert check_answer("There were 12 reports.", res) == ["12"]


def test_list_commas_do_not_join_numbers():
    res = _res(text="counts: 5,12")
    assert check_answer("There were 512 reports.", res) == ["512"]
    assert check_answer("There were 12 reports.", res) == []


def test_truncated_identifiers_are_flagged():
    res = _res(text="NDC 12345-678-90 and NCT01234567")
    assert check_answer("NDC 12345-678-9", res) == ["12345-678-9"]
    psusa = "EMEA/H/C/PSUSA/00001/2020"
    assert check_answer(psusa, res) == [psusa]


def test_two_segment_ndc_is_checked():
    assert check_answer("NDC 0002-1433.", _res(text="NDC 0002-1433")) == []
    assert check_answer("NDC 0002-1434.", _res(text="NDC 0002-1433")) == ["0002-1434"]
    assert check_answer("From 2015-2020.", _res()) == []


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


def test_trigger_terms_need_word_boundaries():
    resolve = _resolve_for("metformin")
    assert route_claim("Metformin and caesarean sectional delivery", resolve) == ("model", None)
    assert route_claim("Metformin case reportsx suggest benefit", resolve) == ("model", None)


def test_plural_trigger_words_are_not_candidates():
    seen = []

    def resolve(text):
        seen.append(text.lower())
        return LookupResult((), Coverage(0))

    route_claim("Were there recalls and trials and referrals for aspirin?", resolve)
    assert seen == ["aspirin"]


def test_max_candidates_is_a_parameter():
    seen = []

    def resolve(text):
        seen.append(text)
        return LookupResult((), Coverage(0))

    route_claim("Is aspirin ibuprofen naproxen approved?", resolve, max_candidates=2)
    assert len(seen) == 2


def test_check_answer_accepts_release_ids_and_known_values():
    from datetime import date as _d

    from episteme.harness.check import check_answer as ca
    from episteme.reference.claims import Claim, Coverage, LookupResult, Provenance

    c = Claim("x", None, "fetched", Provenance("s", "2026-11-02", None, "k", None), 1.0, "b")
    r = LookupResult((c,), Coverage(1))
    assert ca("release 2026-11-02", [r]) == []
    assert ca("from 2023-03-15", [r]) == ["2023-03-15"]
    assert ca("from 2023-03-15", [r], known=["2023-03-15"]) == []
    assert _d
