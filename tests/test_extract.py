from bs4 import BeautifulSoup

from conftest import fixture_text
from leadgen.extract.emails import clean_email, emails_from_mailto, emails_from_text, has_obfuscated_email
from leadgen.extract.industry import IndustryMap
from leadgen.extract.jsonld import parse_jsonld
from leadgen.extract.social import classify_social, extract_socials


class TestEmails:
    def test_clean_email_filters_junk(self):
        assert clean_email(" Info@Acme.com. ") == "info@acme.com"
        assert clean_email("logo@2x.png") is None
        assert clean_email("noreply@acme.com") is None
        assert clean_email("user@example.com") is None
        assert clean_email("0123456789abcdef0123456789abcdef@o1.ingest.sentry.io") is None
        assert clean_email("a@@b.com") is None
        assert clean_email("john..doe@acme.com") is None

    def test_mailto_parsing(self):
        hrefs = ["mailto:Jane.Smith@acme.co.uk?subject=Hi", "mailto:a@acme.com,b@acme.com", "/contact"]
        assert emails_from_mailto(hrefs) == ["jane.smith@acme.co.uk", "a@acme.com", "b@acme.com"]

    def test_text_extraction_skips_assets_and_obfuscation(self):
        text = "Contact info@acme.com or logo@2x.png; reception [at] acme [dot] com"
        assert emails_from_text(text) == ["info@acme.com"]

    def test_obfuscation_detected_not_decoded(self):
        assert has_obfuscated_email('<span class="__cf_email__" data-cfemail="1a2b">[email protected]</span>')
        assert has_obfuscated_email("reception [at] acme [dot] com")
        assert not has_obfuscated_email("<p>info@acme.com</p>")


class TestSocial:
    def test_classify(self):
        assert classify_social("https://uk.linkedin.com/company/acme-ltd/about") == (
            "linkedin_company",
            "https://www.linkedin.com/company/acme-ltd",
        )
        assert classify_social("https://www.linkedin.com/in/jane-smith-123") == (
            "linkedin_person",
            "https://www.linkedin.com/in/jane-smith-123",
        )
        assert classify_social("https://twitter.com/AcmeUK") == ("twitter", "https://x.com/AcmeUK")
        assert classify_social("https://x.com/intent/tweet?text=hi") is None
        assert classify_social("https://m.facebook.com/AcmeDental/") == (
            "facebook",
            "https://www.facebook.com/AcmeDental",
        )
        assert classify_social("https://www.facebook.com/sharer/sharer.php?u=x") is None
        assert classify_social("https://facebook.com/profile.php?id=12345") == (
            "facebook",
            "https://www.facebook.com/profile.php?id=12345",
        )
        assert classify_social("https://www.instagram.com/p/abc123/") is None
        assert classify_social("https://www.instagram.com/acme.dental/") == (
            "instagram",
            "https://www.instagram.com/acme.dental",
        )
        assert classify_social("https://www.facebook.com/wix") is None  # template default

    def test_first_wins(self):
        found = extract_socials(
            ["https://twitter.com/share", "https://twitter.com/first", "https://twitter.com/second"]
        )
        assert found == {"twitter": "https://x.com/first"}


class TestJsonLd:
    def test_parse_org_people_and_address(self):
        info = parse_jsonld(BeautifulSoup(fixture_text("acme_home.html"), "lxml"))
        assert info.org_name == "Acme Dental Ltd"
        assert "Dentist" in info.org_types
        assert info.city == "Bristol" and info.postcode == "BS1 1AA" and info.country == "GB"
        assert info.telephones == ["0117 496 0000"]
        assert [p.name for p in info.people] == ["Jane Smith"]  # review author excluded
        assert info.people[0].job_title.startswith("Owner")
        assert "https://www.linkedin.com/company/acme-dental-bristol/" in info.same_as

    def test_invalid_json_is_ignored(self):
        html = '<script type="application/ld+json">{not json</script>'
        assert parse_jsonld(BeautifulSoup(html, "lxml")).org_name == ""

    def test_trailing_comma_tolerated(self):
        html = '<script type="application/ld+json">{"@type": "Organization", "name": "X Co", "email": "hi@x.com",}</script>'
        info = parse_jsonld(BeautifulSoup(html, "lxml"))
        assert info.org_name == "X Co" and info.emails == ["hi@x.com"]


class TestIndustry:
    def test_classification_precedence(self):
        im = IndustryMap.load()
        assert im.classify(["amenity=dentist"], "plumber") == "Healthcare / Dental"
        assert im.classify(["dental_clinic"]) == "Healthcare / Dental"
        assert im.classify([], "dental clinic") == "Healthcare / Dental"
        assert im.classify([], "anything", override="Custom") == "Custom"
        assert im.classify([], "", schema_types=["LegalService"]) == "Legal Services"
        assert im.classify(["shop=bicycle"]) == "Bicycle"
        assert im.osm_tags_for("law firm") == ["office=lawyer", "office=notary"]
        assert im.osm_tags_for("zzz unknown") == []
