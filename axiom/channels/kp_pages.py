"""Персональная страница КП (telegra.ph) под КАЖДЫЙ аккаунт.

ЗАЧЕМ. Одна общая ссылка на всю армию — это готовый общий след. Массовая рассылка
одинакового URL с десяти номеров связывает их в одну группу: жалоба на одного даёт
повод приложить остальных. Плюс единая точка отказа — у каждой страницы telegra.ph
есть кнопка «Report Page», и когда ссылка одна, репорт убивает её сразу у всех.

Поэтому: у каждого аккаунта СВОЯ страница — свой URL, своё имя автора (его же tg_name),
ссылка на его же профиль и слегка разный текст (заголовок/подводка/формулировки
выбираются по account_id, детерминированно — повторный прогон не плодит новые страницы
с новым текстом). Совпадающего следа не остаётся.

Ссылка и токен правки лежат в accounts.kp_link / accounts.kp_token.

    python -m channels.kp_pages --campaign 9406      # всем аккаунтам команды кампании
    python -m channels.kp_pages --ids 1,9313         # конкретным
    python -m channels.kp_pages --campaign 9406 --force   # перевыпустить (новые URL)
    python -m channels.kp_pages --list               # показать, у кого что есть
"""
from __future__ import annotations

import argparse
import json
import random
import urllib.parse
import urllib.request

from db import database

API = "https://api.telegra.ph/"
TIMEOUT = 30


def _call(method: str, **params) -> dict:
    data = urllib.parse.urlencode(
        {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v)
         for k, v in params.items()}).encode()
    with urllib.request.urlopen(API + method, data=data, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


# --- узлы Telegraph (разрешённые теги: a,b,i,p,h3,h4,ul,li,hr,br,blockquote,…) ---
def _p(*kids):       return {"tag": "p", "children": list(kids)}
def _h3(t):          return {"tag": "h3", "children": [t]}
def _h4(t):          return {"tag": "h4", "children": [t]}
def _b(t):           return {"tag": "b", "children": [t]}
def _i(t):           return {"tag": "i", "children": [t]}
def _a(t, href):     return {"tag": "a", "attrs": {"href": href}, "children": [t]}
def _br():           return {"tag": "br"}
_HR = {"tag": "hr"}


# Варианты формулировок. Выбор — по account_id через seed, а не random.choice() на
# каждый вызов: иначе повторный прогон менял бы текст уже опубликованной страницы.
TITLES = [
    "Нейросотрудники для бизнеса",
    "ИИ в продажах, маркетинге и процессах",
    "Нейросотрудники: продажи, маркетинг, рутина",
    "Где бизнес теряет деньги и что с этим делает ИИ",
]
HEADLINES = [
    "Сотрудники, которые не спят, не выгорают и не увольняются",
    "Работают круглосуточно, не выгорают и не просят отпуск",
    "Нейросотрудник закрывает участок, на котором вы теряете деньги",
    "Не нейросети ради нейросетей — закрытие дыр, через которые утекает выручка",
]
LEDES = [
    "Я не продаю нейросети. Я ставлю в бизнес нейросотрудников — в продажи, маркетинг "
    "и рутинные процессы — туда, где сейчас утекают заявки, время и выручка. "
    "Разговор начинается с диагностики, а не с прайса.",
    "Моя работа — не «внедрить ИИ», а закрыть конкретные места, где бизнес теряет "
    "заявки, время и деньги. Начинаем не с прайса, а с разбора ваших процессов.",
    "Ставлю нейросотрудников туда, где есть измеримая потеря: медленные ответы, "
    "перегруженные менеджеры, мёртвая база, дорогой эксперт на типовых вопросах. "
    "Сначала диагностика, потом решение.",
    "Не продаю технологию — закрываю дыры, через которые уходят деньги: в продажах, "
    "в маркетинге и в рутине. Первый шаг — посчитать, во что эти дыры обходятся.",
]
CLOSERS = [
    "Напишите одно слово — «диагностика». Этого достаточно, чтобы начать.",
    "Напишите «диагностика» — этого хватит, чтобы договориться о разборе.",
    "Одно слово в ответ — «диагностика», и назначим разбор.",
    "Хватит одного слова — «диагностика».",
]

LEAKS = [
    ("Клиент написал вечером, ответили утром", "минус сделка",
     "За ночь он написал ещё троим. Отвечает тот, кто ответил первым, а не тот, кто лучше."),
    ("Менеджер ведёт 40 диалогов и выгорает", "минус 30% конверсии",
     "К сороковому он отвечает односложно. Первым десяти повезло, остальным нет."),
    ("База лежит мёртвым грузом", "минус весь актив",
     "Сотни контактов, которые когда-то интересовались. Руки до них не доходят никогда."),
    ("Эксперт тратит часы на однотипные вопросы", "минус самый дорогой час",
     "Одни и те же двадцать вопросов. Отвечать на них должен не тот, кто дороже всех стоит."),
]

LINES = [
    ("Продажи",
     "Ведёт переписку в Telegram и WhatsApp: отвечает за секунды, снимает возражения, "
     "доводит до созвона и передаёт вам уже тёплого клиента. Одинаково ровно на первом "
     "и на сотом диалоге."),
    ("Маркетинг",
     "Ищет вашу аудиторию там, где она сама говорит о своей задаче, пишет первым и "
     "приводит заявки. Плюс контент и прогрев базы, которая сейчас лежит мёртвым грузом."),
    ("Автоматизация процессов",
     "Разбор входящих, документы, отчёты, ответы на типовые вопросы, передача данных "
     "между системами. Всё, что съедает часы и не требует головы."),
    ("Ваша экспертиза как продукт",
     "Если есть знания и поток людей, которым они нужны, — упаковываем это в систему, "
     "которая работает без вашего участия в каждом разговоре."),
]


def _build(acc: dict, rnd: random.Random) -> tuple[str, list]:
    """(title, content) — персонально под аккаунт."""
    who = (acc.get("tg_name") or acc.get("label") or "").strip()
    uname = (acc.get("username") or "").strip().lstrip("@")
    tg_url = f"https://t.me/{uname}" if uname else None

    leaks = LEAKS[:]
    rnd.shuffle(leaks)       # порядок пунктов тоже свой у каждого
    lines = LINES[:]         # направления оставляем по порядку — 01..04 это логика, не украшение

    content = [
        _h3(rnd.choice(HEADLINES)),
        _p(rnd.choice(LEDES)),
        _HR,
        _h3("Четыре утечки, которые есть почти у всех"),
        _p(_i("Ни одна из них не выглядит как проблема. Каждая стоит денег ежедневно.")),
    ]
    for what, cost, note in leaks:
        content.append(_p(_b(what), " — ", _i(cost), _br(), note))

    content += [
        _HR,
        _h3("Нейросотрудники по направлениям"),
        _p(_i("Каждый закрывает свой участок. Ставятся по одному — начинаем с того, "
              "где болит сильнее.")),
    ]
    for n, (head, body) in enumerate(lines, 1):
        content.append(_h4(f"{n:02d} · {head}"))
        content.append(_p(body))

    content += [
        _HR,
        _h3("Диагностика: где вы теряете деньги"),
        _p(_b("Бесплатно, в подарок."),
           " Созвон 30 минут. Разбираем ваши процессы и считаем, во что обходятся дыры."),
        {"tag": "ul", "children": [
            {"tag": "li", "children": ["Карта утечек: где именно теряются заявки, время и выручка"]},
            {"tag": "li", "children": ["Оценка в деньгах — сколько стоит каждая дыра за месяц"]},
            {"tag": "li", "children": ["План: что закрывается автоматизацией сейчас, что позже, что не трогать"]},
            {"tag": "li", "children": ["Честный ответ, нужен ли вам ИИ вообще"]},
        ]},
        _p(_i("Если задача решается без меня — так и скажу. Это сэкономит время нам обоим, "
              "а вам ещё и бюджет.")),
        _HR,
        _h3("Написать напрямую"),
    ]
    # Контакт — ТОГО аккаунта, с которого пришла ссылка: человек пишет туда же, где
    # его читает. Чужой контакт на странице ломает доверие и путает маршрут ответа.
    if tg_url:
        content.append(_p(_b("Telegram: "), _a(f"@{uname}", tg_url)))
    else:
        content.append(_p("Ответьте прямо в этот диалог — отвечаю лично."))
    content.append(_p(_b(rnd.choice(CLOSERS))))

    title = rnd.choice(TITLES)
    return title, content


def _accounts(campaign_id: int | None, ids: list[int] | None) -> list[dict]:
    with database.get_conn() as conn:
        if ids:
            ph = ",".join("?" * len(ids))
            rows = conn.execute(
                f"SELECT * FROM accounts WHERE id IN ({ph}) ORDER BY id", ids).fetchall()
        elif campaign_id:
            rows = conn.execute(
                "SELECT a.* FROM campaign_accounts ca JOIN accounts a ON a.id=ca.account_id "
                "WHERE ca.campaign_id=? ORDER BY a.id", (campaign_id,)).fetchall()
        else:
            rows = []
    return [dict(r) for r in rows]


def publish(acc: dict, force: bool = False) -> dict:
    """Создаёт страницу под аккаунт. Уже есть и не --force → ничего не делает."""
    if acc.get("kp_link") and not force:
        return {"id": acc["id"], "skipped": True, "url": acc["kp_link"]}
    # seed по id: текст у аккаунта всегда один и тот же между прогонами
    rnd = random.Random(f"kp-{acc['id']}")
    title, content = _build(acc, rnd)
    author = (acc.get("tg_name") or acc.get("label") or "").strip()
    uname = (acc.get("username") or "").strip().lstrip("@")

    a = _call("createAccount", short_name=(author.split()[0] if author else "author")[:32],
              author_name=author[:128],
              **({"author_url": f"https://t.me/{uname}"} if uname else {}))
    if not a.get("ok"):
        return {"id": acc["id"], "error": f"createAccount: {a}"}
    token = a["result"]["access_token"]

    r = _call("createPage", access_token=token, title=title[:256],
              author_name=author[:128],
              **({"author_url": f"https://t.me/{uname}"} if uname else {}),
              content=content, return_content="false")
    if not r.get("ok"):
        return {"id": acc["id"], "error": f"createPage: {r}"}
    url = r["result"]["url"]
    with database.get_conn() as conn:
        conn.execute("UPDATE accounts SET kp_link=?, kp_token=? WHERE id=?",
                     (url, token, acc["id"]))
    return {"id": acc["id"], "label": acc.get("label"), "url": url, "title": title}


def run(campaign_id: int | None, ids: list[int] | None, force: bool) -> dict:
    database.init_db()
    accs = _accounts(campaign_id, ids)
    if not accs:
        return {"ok": False, "error": "аккаунты не найдены (проверь --campaign/--ids)"}
    out = [publish(a, force) for a in accs]
    made = [o for o in out if o.get("url") and not o.get("skipped")]
    return {"ok": True, "total": len(out), "created": len(made), "results": out}


def show() -> None:
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, label, tg_name, username, kp_link FROM accounts "
            "WHERE kp_link IS NOT NULL AND kp_link<>'' ORDER BY id").fetchall()
    if not rows:
        print("персональных страниц КП пока нет")
        return
    for r in rows:
        print(f"  #{r['id']:<6} {(r['label'] or ''):<14} {(r['tg_name'] or ''):<22} {r['kp_link']}")


def main() -> None:
    p = argparse.ArgumentParser(description="Персональные страницы КП (telegra.ph) под аккаунты")
    p.add_argument("--campaign", type=int, default=None, help="всем аккаунтам команды кампании")
    p.add_argument("--ids", default=None, help="конкретные аккаунты, id через запятую")
    p.add_argument("--force", action="store_true", help="перевыпустить, даже если ссылка уже есть")
    p.add_argument("--list", action="store_true", dest="do_list", help="показать текущие ссылки")
    args = p.parse_args()
    if args.do_list:
        database.init_db()
        show()
        return
    ids = [int(x) for x in args.ids.split(",") if x.strip().isdigit()] if args.ids else None
    if not ids and not args.campaign:
        p.error("укажи --campaign <id> или --ids 1,2,3")
    print(json.dumps(run(args.campaign, ids, args.force), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
