"""Offline: the carousel section counts the norm and the spike from sheet rows."""
import unittest
from unittest.mock import patch

import gspread

from integrations import content_carousels as cc

POSTS_HEAD = ["аккаунт", "дата", "ссылка", "формат", "тема", "слайдов", "лайки", "комменты", "просмотры",
              "охват", "сохранения", "репосты", "картинка", "обновлено"]
TRENDS_HEAD = ["id", "дата", "площадка", "формат", "автор", "тема", "всплеск", "охват", "реакции", "репосты", "боль", "угол",
               "ссылка", "взято", "оценка", "комментарий", "картинка", "комменты", "норма"]


def post(account, n, likes, comments=0, fmt="карусель", views=""):
    return [account, f"2026-10-{n:02d} 10:00", f"https://www.instagram.com/p/{account}{n}/", fmt, f"тема {n}", "8",
            str(likes), str(comments), str(views), "", "", "", "", "2026-10-14 06:00"]


def sheets(**tables):
    def rows(name):
        if name not in tables:
            raise gspread.exceptions.WorksheetNotFound(name)
        return tables[name]
    return patch.object(cc.content_sheet, "_rows", side_effect=rows)


class SummaryTests(unittest.TestCase):
    def test_spike_against_the_account_norm(self):
        rows = [POSTS_HEAD] + [post("@a", n, 10) for n in range(1, 6)] + [post("@a", 6, 28, 4)]
        with sheets(**{"ИНСТАГРАМ": rows}):
            posts = cc.summary()["posts"]
        top = posts[-1]
        self.assertEqual((top["metric"], top["norm"], top["spike"], top["hit"]), (40, 10, 4.0, True))
        self.assertFalse(posts[0]["hit"])

    def test_few_posts_give_no_norm(self):
        with sheets(**{"ИНСТАГРАМ": [POSTS_HEAD] + [post("@a", n, 10) for n in range(1, 4)]}):
            posts = cc.summary()["posts"]
        self.assertTrue(all(p["spike"] is None and not p["hit"] for p in posts))

    def test_accounts_and_formats_have_separate_norms(self):
        rows = ([POSTS_HEAD] + [post("@a", n, 10) for n in range(1, 6)] + [post("@b", n, 100) for n in range(1, 6)]
                + [post("@a", n, 1, fmt="видео", views=500) for n in range(6, 11)])
        with sheets(**{"ИНСТАГРАМ": rows}):
            posts = cc.summary()["posts"]
        norms = {(p["account"], p["format"]): p["norm"] for p in posts}
        self.assertEqual(norms, {("@a", "карусель"): 10, ("@b", "карусель"): 100, ("@a", "видео"): 500})

    def test_no_sheet_yet_is_an_empty_summary(self):
        with sheets():
            s = cc.summary()
        self.assertEqual((s["posts"], s["accounts"], s["updated"]), ([], [], ""))


class SourcesTests(unittest.TestCase):
    def test_only_instagram_carousels_of_donors(self):
        def trend(platform, fmt, author, spike):
            row = dict.fromkeys(TRENDS_HEAD, "")
            row.update({"площадка": platform, "формат": fmt, "автор": author, "всплеск": spike, "реакции": "1 200",
                        "комменты": "30", "норма": "150", "ссылка": f"https://x/{author}{fmt}"})
            return [row[k] for k in TRENDS_HEAD]
        rows = [TRENDS_HEAD, trend("instagram", "карусель", "donor", "4,2×"), trend("instagram", "видео", "donor", "9×"),
                trend("youtube", "видео", "yt", "5×")]
        donors = [cc.ACCOUNT_HEADER, ["instagram", "@donor", "https://www.instagram.com/donor/", "active", "", ""],
                  ["youtube", "@yt", "https://youtube.com/@yt", "active", "", ""]]
        with sheets(**{"ТРЕНДЫ": rows, "ДОНОРЫ": donors, "ПРОГОНЫ": [[]]}):
            s = cc.sources()
        self.assertEqual([(i["author"], i["spike"], i["likes"], i["comments"], i["norm"]) for i in s["items"]],
                         [("donor", 4.2, 1200, 30, 150)])
        self.assertEqual([d["name"] for d in s["donors"]], ["@donor"])


if __name__ == "__main__":
    unittest.main()
