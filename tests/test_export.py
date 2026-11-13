from ads_leads.export import priority, private_mobiles
from ads_leads.models import Company
from ads_leads.site_crawl import is_mobile


def company(**kw) -> Company:
    base = {"domain": "x.ru", "ad_cost": 2_000_000}  # 2 млн коп./день ≈ 600 тыс. ₽/мес
    base.update(kw)
    return Company(**base)


def test_priority_rules():
    assert priority(company(signals="roistat", revenue=900_000_000))[0] == "A"
    assert priority(company(signals="calltouch"))[0] == "A"  # коллтрекинг, выручка неизвестна
    assert priority(company(signals="metrika", revenue=900_000_000))[0] == "B"  # нет аналитики
    assert priority(company(signals="roistat", revenue=50_000_000))[0] == "B"  # мелкая
    assert priority(company(signals="roistat", ad_cost=500_000))[0] == "C"  # 150 тыс. ₽/мес
    assert priority(company(signals="roistat", ad_cost=None))[0] == "C"
    assert priority(company(signals="roistat", legal_status="Ликвидировано"))[0] == "C"


def test_private_mobiles():
    assert is_mobile("+7 916 123-45-67") and not is_mobile("+7 495 125-40-40") and not is_mobile("8 800 555-35-35")
    c = company(phones="+7 916 111-22-33; +7 495 125-40-40",
                kontur_phones="+7 (916) 111-22-33; +7 903 214-58-17; +7 495 125-40-40")
    assert private_mobiles(c) == ["+7 903 214-58-17"]  # номер с сайта — общий, городской — не мобильный
