from leadgen.compliance import SuppressionList, parse_identifier, suppression_hash
from leadgen.dedupe import DedupeIndex, dedupe_keys, make_lead_id
from leadgen.models import EmailStatus, EmailType, Lead
from leadgen.normalize import (
    country_name,
    normalize_company,
    normalize_phone,
    registered_domain,
    split_location,
)
from leadgen.validate.email import email_type, is_low_value, rank


class TestNormalize:
    def test_company_names(self):
        assert normalize_company("The Smile Dental Co., Ltd.") == "smile dental"
        assert normalize_company("Smile Dental") == "smile dental"
        assert normalize_company("Müller & Söhne GmbH") == "muller sohne"
        assert normalize_company("Ltd") == "ltd"  # never normalizes to empty

    def test_domains(self):
        assert registered_domain("https://www.Acme.co.uk/contact?x=1") == "acme.co.uk"
        assert registered_domain("shop.acme.com") == "acme.com"
        assert registered_domain("https://smile.github.io") == "smile.github.io"
        assert registered_domain("https://smile.business.site") == "smile.business.site"
        assert registered_domain("http://192.168.1.1") is None

    def test_country_and_location(self):
        assert country_name("UK") == country_name("GB") == "United Kingdom"
        assert country_name("usa") == "United States"
        assert split_location("Leeds, West Yorkshire, UK") == ("Leeds", "United Kingdom")
        assert split_location("Berlin") == ("Berlin", "")

    def test_phone(self):
        assert normalize_phone("0117 496 0000", "United Kingdom") == "+441174960000"
        assert normalize_phone("n/a") == ""


class TestDedupe:
    def test_keys(self):
        keys = dedupe_keys(
            email="Info@Acme.com", website="https://www.acme.com/x", company_name="Acme Ltd", city="Bristol"
        )
        assert keys == ["email:info@acme.com", "domain:acme.com", "name:acme|bristol"]
        assert dedupe_keys(company_name="Acme") == []  # name without city is too weak

    def test_index_find_and_ignore(self):
        idx = DedupeIndex.from_leads(
            [Lead(lead_id="ld_1", domain="acme.com", company_name="Acme", city="Bristol")]
        )
        assert idx.find(["domain:acme.com"]) == "ld_1"
        assert idx.find(["name:acme|bristol"]) == "ld_1"
        assert idx.find(["name:acme|leeds"]) is None
        assert idx.find(["domain:acme.com"], ignore="ld_1") is None

    def test_lead_id_stable_and_domain_first(self):
        a = make_lead_id(["email:x@acme.com", "domain:acme.com"])
        b = make_lead_id(["domain:acme.com"])
        assert a == b and a.startswith("ld_") and len(a) == 15


class TestSuppression:
    def test_hashing_is_case_insensitive(self):
        s = SuppressionList({suppression_hash("email", "Jane@Acme.com")})
        assert s.email_suppressed("jane@acme.com")
        assert not s.email_suppressed("info@acme.com")
        assert not s.domain_suppressed(None)

    def test_parse_identifier(self):
        assert parse_identifier("Jane@Acme.com") == ("email", "jane@acme.com")
        assert parse_identifier("https://www.acme.co.uk/about") == ("domain", "acme.co.uk")


class TestEmailValidation:
    async def test_statuses(self, validator):
        site = "acme.com"
        c = await validator.check("Jane.Doe@acme.com", site)
        assert (c.status, c.type, c.matches_site) == (EmailStatus.VALID, EmailType.PERSONAL, True)
        assert (await validator.check("info@acme.com", site)).type == EmailType.ROLE
        assert (await validator.check("acme.dental@gmail.com", site)).status == EmailStatus.RISKY
        assert (await validator.check("x@mailinator.com", site)).status == EmailStatus.INVALID
        assert (await validator.check("x@deadmail.com", None)).status == EmailStatus.INVALID
        assert (await validator.check("x@flaky-dns.com", None)).status == EmailStatus.RISKY
        assert (await validator.check("x@aonly.com", None)).status == EmailStatus.RISKY
        assert (await validator.check("sales@agency.com", site)).reason == "domain differs from website"
        assert (await validator.check("not-an-email", site)).status == EmailStatus.INVALID

    def test_email_type(self):
        assert email_type("info2@acme.com") == EmailType.ROLE
        assert email_type("sales.uk@acme.com") == EmailType.ROLE
        assert email_type("j.smith@acme.com") == EmailType.PERSONAL
        assert email_type("j.smith@outlook.com") == EmailType.FREE_MAIL
        # department / mailing-list addresses are shared mailboxes, not people
        assert email_type("python-announce@python.org") == EmailType.ROLE
        assert email_type("security@postgresql.org") == EmailType.ROLE
        assert email_type("dev.patel@acme.com") == EmailType.PERSONAL
        assert is_low_value("python-announce@python.org") and not is_low_value("info@acme.com")

    async def test_rank_prefers_site_personal_then_role_and_demotes_careers(self, validator):
        checks = await validator.check_many(
            ["careers@acme.com", "info@acme.com", "jane@acme.com", "jane@gmail.com", "x@deadmail.com"],
            "acme.com",
        )
        ranked = [c.email for c in rank(checks)]
        assert ranked == ["jane@acme.com", "info@acme.com", "careers@acme.com", "jane@gmail.com"]
        ranked_role_pref = [c.email for c in rank(checks, prefer_personal=False)]
        assert ranked_role_pref[0] in ("jane@acme.com", "info@acme.com")
