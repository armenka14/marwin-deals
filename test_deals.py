"""Тесты marwin_deals.py без сети. Запуск: python test_deals.py"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import marwin_deals as m  # noqa: E402


def item(i, cur, old, stock=True, name=None):
    return {"id": i, "name": name or f"Товар {i}", "slug": f"tovar-{i}", "image": f"https://img/{i}.jpg",
            "price": {"current": cur, "old": old}, "badges": {"discount": None}, "in_stock": stock}


class FakeApi:
    def __init__(self, cats):
        self.cats = cats  # {cat_id: [items]}
        self.detail_calls = []

    def top_categories(self):
        return list(self.cats)

    def discounted(self, cat, city, in_stock_only):
        return iter(self.cats[cat])

    def details(self, pid, city):
        self.detail_calls.append(pid)
        return {"description": "Отличный товар. Очень полезный и красивый, подходит для подарка."}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        m.STATE_PATH = Path(self.tmp.name) / "seen.json"
        m.LOG_PATH = Path(self.tmp.name) / "deals.log"
        self.cfg = dict(m.CONFIG, bot_token="t", channel="@c", max_posts=40, first_run_posts=2)
        self.sent = []
        self.p = mock.patch.object(m, "tg", lambda cfg, meth, payload: self.sent.append((meth, payload)) or {"ok": True})
        self.p.start()
        self.pd = mock.patch.object(m, "download", lambda url: None)  # без сети
        self.pd.start()

    def tearDown(self):
        self.p.stop()
        self.pd.stop()
        self.tmp.cleanup()

    def photos(self):
        return [p for meth, p in self.sent if meth == "sendPhoto"]


class TestDeals(Base):
    def test_discount_calc_and_threshold(self):
        self.assertEqual(m.discount_of(item(1, 900, 1000)), 10)
        api = FakeApi({1: [item(1, 900, 1000), item(2, 890, 1000), item(3, 500, 1000)]})
        found = m.collect(api, self.cfg)
        self.assertEqual(sorted(found), ["2", "3"])  # ровно 10% не берём, нужно больше

    def test_out_of_stock_skipped_and_dedup(self):
        api = FakeApi({1: [item(1, 500, 1000, stock=False), item(2, 500, 1000)], 2: [item(2, 500, 1000)]})
        self.assertEqual(list(m.collect(api, self.cfg)), ["2"])

    def test_first_run_posts_top_and_marks_rest(self):
        api = FakeApi({1: [item(i, 1000 - i * 50, 1000) for i in range(3, 9)]})
        m.run(api, self.cfg, sleep=lambda s: None)
        self.assertEqual(len(self.photos()), 2)
        self.assertIn("−40%", self.photos()[0]["caption"])        # сначала самая большая скидка
        self.assertEqual(len(m.load_state()), 6)                  # остальные помечены как известные
        self.sent.clear()
        m.run(api, self.cfg, sleep=lambda s: None)                # второй запуск: ничего нового
        self.assertEqual(self.photos(), [])

    def test_new_and_bigger_discount_posted_once(self):
        cats = {1: [item(1, 800, 1000)]}
        api = FakeApi(cats)
        m.save_state({})
        m.run(api, self.cfg, sleep=lambda s: None)
        self.assertEqual(len(self.photos()), 1)
        m.run(api, self.cfg, sleep=lambda s: None)
        self.assertEqual(len(self.photos()), 1)                   # повтор не шлём
        cats[1] = [item(1, 600, 1000)]                            # скидка выросла 20% -> 40%
        m.run(api, self.cfg, sleep=lambda s: None)
        self.assertEqual(len(self.photos()), 2)
        cats[1] = []                                              # скидка закончилась
        m.run(api, self.cfg, sleep=lambda s: None)
        self.assertEqual(m.load_state(), {})
        cats[1] = [item(1, 800, 1000)]                            # вернулась -> снова пост
        m.run(api, self.cfg, sleep=lambda s: None)
        self.assertEqual(len(self.photos()), 3)

    def test_max_posts_queue(self):
        m.save_state({})
        self.cfg["max_posts"] = 3
        api = FakeApi({1: [item(i, 500, 1000) for i in range(5)]})
        m.run(api, self.cfg, sleep=lambda s: None)
        self.assertEqual(len(self.photos()), 3)
        m.run(api, self.cfg, sleep=lambda s: None)                # остаток уходит в следующий запуск
        self.assertEqual(len(self.photos()), 5)

    def test_caption_format(self):
        it = dict(item(7, 38990, 59950, name='LEGO <Icons> "Беззубик"'), discount=35)
        cap = m.caption(it, "А" * 500)
        self.assertIn("−35%", cap)
        self.assertIn("38 990 ₸", cap)
        self.assertIn("<s>59 950 ₸</s>", cap)
        self.assertIn("&lt;Icons&gt;", cap)                       # HTML экранирован
        self.assertIn('href="https://new.marwin.kz/product/tovar-7"', cap)
        self.assertLessEqual(len(cap), 1024)

    def test_short_description(self):
        self.assertEqual(m.short_description("Коротко."), "Коротко.")
        long = "Первое предложение довольно длинное и содержательное, рассказывает о товаре подробно. " * 5
        self.assertTrue(m.short_description(long).endswith("."))
        self.assertLessEqual(len(m.short_description(long)), 220)

    def test_api_failure_keeps_state(self):
        m.save_state({"1": 20})

        class Down(FakeApi):
            def discounted(self, *a):
                raise RuntimeError("down")
        with self.assertRaises(RuntimeError):
            m.run(Down({1: []}), self.cfg, sleep=lambda s: None)
        self.assertEqual(m.load_state(), {"1": 20})               # при сбое состояние не стирается


if __name__ == "__main__":
    unittest.main(verbosity=2)
