from ads_leads.keywords import build_filter, canonical, is_noise, preset_seeds, suggest_queries


class FakeClient:
    def __init__(self, rows):
        self.rows, self.filters = rows, []

    def keyword_base(self, filter_text):
        self.filters.append(filter_text)
        return self.rows


def test_preset_and_filter():
    assert "смеситель" in preset_seeds("Сантехника")
    assert preset_seeds("Велосипеды") == ["велосипеды"]
    f = build_filter("смеситель")
    assert f.startswith("wordLIKEсмеситель купить") and "\n-авито" in f


def test_noise():
    assert canonical("купить смеситель для кухни") == canonical("смеситель для кухни купить")
    assert is_noise("душевая кабина купить в барнауле", ["msk"])
    assert is_noise("купить смеситель для кухни в спб", ["msk"])
    assert not is_noise("купить смеситель для кухни в спб", ["msk", "spb"])
    assert not is_noise("смеситель купить в москве", ["msk"])
    assert is_noise("92730000 hansgrohe картридж купить", ["msk"])
    assert is_noise("посуда villeroy boch купить", ["msk"])
    assert not is_noise("душевой трап под плитку купить", ["msk"])  # «под плитку» — нормальный товар


def test_suggest_dedup_and_threshold():
    rows = [
        {"word": "купить смеситель для кухни", "wsk": 2000},
        {"word": "смеситель для кухни купить", "wsk": 1900},  # тот же набор слов
        {"word": "смеситель купить в спб", "wsk": 500},
        {"word": "смеситель купить", "wsk": 300},
        {"word": "смеситель редкий купить", "wsk": 5},  # ниже порога — дальше не идём
        {"word": "смеситель другой купить", "wsk": 4},
    ]
    client = FakeClient(rows)
    res = suggest_queries(client, ["смеситель"], ["msk"], per_seed=10, min_freq=20)
    assert [s.text for s in res] == ["купить смеситель для кухни", "смеситель купить"]
    assert len(client.filters) == 1


def test_new_presets():
    assert "духи" in preset_seeds("Парфюмерия") and "духи" in preset_seeds("духи")
    assert "люстра" in preset_seeds("Свет: люстры и светильники") and "люстра" in preset_seeds("освещение")
    assert "кондиционер" in preset_seeds("кондиционеры") and "матрас" in preset_seeds("Матрасы")
