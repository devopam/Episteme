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
    from episteme.harness.check import check_answer as ca
    from episteme.reference.claims import Claim, Coverage, LookupResult, Provenance

    c = Claim("x", None, "fetched", Provenance("s", "2026-11-02", None, "k", None), 1.0, "b")
    r = LookupResult((c,), Coverage(1))
    assert ca("release 2026-11-02", [r]) == []
    assert ca("from 2023-03-15", [r]) == ["2023-03-15"]
    assert ca("from 2023-03-15", [r], known=["2023-03-15"]) == []


# --- final review I1: coverage notes and missing datasets are stated text ------------


def _noted(*notes: str, missing: tuple[str, ...] = ()) -> list[LookupResult]:
    return [LookupResult((), Coverage(0, missing=missing, notes=notes))]


def test_coverage_notes_of_the_lookups_are_stated():
    # The real note strings of lookups.py and literature.py.
    res = _noted(
        "fda_enforcement: 37 records with no date not checked",
        "limited to the 20 newest records",
        "substance 4711 not found",
        "substance 4712 has no name to match",
        "no FAERS data for quarters: 2023Q1, 2023Q2",
        missing=("ema_psusa",),
    )
    text = (
        "Coverage note: fda_enforcement: 37 records with no date not checked\n"
        "Coverage note: limited to the 20 newest records\n"
        "Coverage note: substance 4711 not found\n"
        "Coverage note: substance 4712 has no name to match\n"
        "Coverage gap: ema_psusa"
    )
    assert check_answer(text, res) == []
    assert check_answer("38 records with no date", res) == ["38"]


# --- final review I3: short PSUSA numbers and slash dates --------------------------


def test_short_psusa_number_is_checked():
    assert check_answer("See PSUSA/00001234/202301.", []) == ["PSUSA/00001234/202301"]
    # The same procedure with and without the EMEA/H/C/ prefix is one number.
    res = _res(key="EMEA/H/C/PSUSA/00001234/202301")
    assert check_answer("See PSUSA/00001234/202301.", res) == []
    assert check_answer("See PSUSA/00009999/202301.", res) == ["PSUSA/00009999/202301"]


def test_long_psusa_number_is_still_checked():
    assert check_answer("EMEA/H/C/PSUSA/00001234/202301", []) == ["EMEA/H/C/PSUSA/00001234/202301"]


def test_year_first_slash_date_is_checked():
    assert check_answer("on 2020/03/03", []) == ["2020/03/03"]
    assert check_answer("on 2020/03/04", _res()) == []


def test_day_or_month_first_slash_date_matches_in_either_order():
    res = _res()  # states 2020-03-04
    assert check_answer("on 03/04/2020", res) == []  # MM/DD/YYYY
    assert check_answer("on 04/03/2020", res) == []  # DD/MM/YYYY
    assert check_answer("on 05/06/2021", res) == ["05/06/2021"]
    assert check_answer("on 3/4/2020", res) == []
    assert check_answer("on 13/25/2020", res) == ["13/25/2020"]


def test_slash_date_in_results_is_stated():
    c = Claim("x", "03/15/2023", "fetched", Provenance("s", None, None, "k", None), 1.0, "b")
    res = [LookupResult((c,), Coverage(1))]
    assert check_answer("on 2023-03-15", res) == []
    assert check_answer("on 15 March 2023", res) == []


def test_ndc_and_nct_still_flagged():
    assert check_answer("NDC 0002-3227", []) == ["0002-3227"]
    assert check_answer("NCT99999999", []) == ["NCT99999999"]


# --- final review M2: max_candidates default from settings -------------------------


def test_route_claim_default_max_candidates_from_settings(monkeypatch):
    import dataclasses

    from episteme import config
    from episteme.harness import check

    s = dataclasses.replace(config.get_settings(), harness_max_candidates=1)
    monkeypatch.setattr(check, "get_settings", lambda: s)
    seen = []

    def resolve(text):
        seen.append(text)
        return LookupResult((), Coverage(0))

    route_claim("Is aspirin ibuprofen naproxen approved?", resolve)
    assert len(seen) == 1
