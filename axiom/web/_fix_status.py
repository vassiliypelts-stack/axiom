import sys, os
sys.stdout.reconfigure(encoding='utf-8')
os.chdir(os.path.dirname(__file__) or ".")

with open("index.html", "r", encoding="utf-8") as f:
    html = f.read()

reps = [
    ('🟢 жив', '<span class="sdot g"></span>жив'),
    ('🔴 сессия слетела', '<span class="sdot r"></span>сессия слетела'),
    ('⛔ бан', '<span class="sdot r"></span>бан'),
    ('🔴 мёртвый', '<span class="sdot r"></span>мёртвый'),
    ('🟢 живой', '<span class="sdot g"></span>живой'),
    ('⚪ не проверен', '<span class="sdot gy"></span>не проверен'),
    ('⚪ не проверено', '<span class="sdot gy"></span>не проверено'),
    ('⚪ нет связи', '<span class="sdot gy"></span>нет связи'),
]
for old, new in reps:
    n = html.count(old)
    if n:
        html = html.replace(old, new)
        print(f"OK: {old} -> {new} ({n}x)")
    else:
        print(f"--: {old} not found")
with open("index.html", "w", encoding="utf-8") as f:
    f.write(html)
print("DONE")
