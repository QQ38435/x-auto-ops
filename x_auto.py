#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
X 全自动运营程序(x_auto)
- 24h 循环:搜集 → 生成文案 → 发帖 / 评论 / 回复
- 使用 Playwright 持久化 profile:首次 --login 手动登录,之后复用登录态
- 风控:随机间隔、每小时上限、每日上限、凌晨静默、去重、失败熔断

用法:
  python x_auto.py --login          # 首次:打开浏览器,登录 X,回车保存
  python x_auto.py                  # 正式运行(24h 循环)
  python x_auto.py --dry-run        # 只搜集+生成文案,不打开浏览器、不发布
  python x_auto.py --once           # 只跑一轮(调试)
"""
import argparse
import hashlib
import json
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
CFG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.json"
LOG_PATH = BASE / "x_auto.log"


# ---------------- 基础设施 ----------------
def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def now():
    return datetime.now()


def today():
    return now().strftime("%Y-%m-%d")


def fingerprint(text):
    return hashlib.md5(text.strip().lower().encode("utf-8")).hexdigest()[:12]


# ---------------- 状态 ----------------
def fresh_state():
    return {"day": today(), "posts": 0, "comments": 0, "replies": 0,
            "hour": now().strftime("%Y-%m-%d %H"), "hour_count": 0,
            "posted_urls": [], "commented_urls": [], "replied_ids": [],
            "texts": [], "fan_count": None}


def load_state():
    st = load_json(STATE_PATH, fresh_state())
    if st.get("day") != today():
        st.update({"day": today(), "posts": 0, "comments": 0, "replies": 0})
    if st.get("hour") != now().strftime("%Y-%m-%d %H"):
        st.update({"hour": now().strftime("%Y-%m-%d %H"), "hour_count": 0})
    return st


def save_state(st):
    save_json(STATE_PATH, st)


# ---------------- 素材源 ----------------
def http_get(url, timeout=25, headers=None):
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_github(keywords, per_page=5):
    """GitHub 搜索:近 N 天活跃、star 排序。"""
    days = CFG.get("sources", {}).get("github_days", 14)
    since = (now().replace(hour=0, minute=0, second=0) - __import__("datetime").timedelta(days=days)).strftime("%Y-%m-%d")
    items = []
    for kw in keywords:
        q = "%s in:name,description pushed:>%s" % (kw, since)
        url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
            {"q": q, "sort": "stars", "order": "desc", "per_page": str(per_page)})
        try:
            data = json.loads(http_get(url, headers={"Accept": "application/vnd.github+json"}).decode())
        except Exception as e:
            log("github err %s: %s" % (kw, e))
            continue
        for it in data.get("items", []):
            items.append({
                "kind": "github", "name": it["full_name"], "stars": it["stargazers_count"],
                "desc": (it.get("description") or "").strip(), "url": it["html_url"],
                "score": min(5, 2 + it["stargazers_count"] // 50000),
            })
    # 去重 + 排序
    uniq = {}
    for it in items:
        uniq[it["url"]] = it
    return sorted(uniq.values(), key=lambda x: -x["stars"])


def fetch_rss(feeds, per_feed=3):
    """RSS/Atom 抓取(空投/币圈/优惠)。"""
    out = []
    for feed_url in feeds:
        try:
            raw = http_get(feed_url, timeout=20)
            root = __import__("xml.etree.ElementTree", fromlist=["ET"]).fromstring(raw)
        except Exception as e:
            log("rss err %s: %s" % (feed_url, e))
            continue
        # RSS: item/title/link ; Atom: entry/title/link@href
        nodes = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for n in nodes[:per_feed]:
            title = (n.findtext("title") or n.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            link = n.findtext("link")
            if not link:
                le = n.find("{http://www.w3.org/2005/Atom}link")
                link = le.get("href") if le is not None else ""
            if title and link:
                out.append({"kind": "rss", "name": title, "url": link, "desc": "", "stars": 0, "score": 3})
    return out


def collect():
    src = CFG.get("sources", {})
    items = []
    if src.get("github_keywords"):
        items += fetch_github(src["github_keywords"])
    if src.get("rss_feeds"):
        items += fetch_rss(src["rss_feeds"])
    # 过滤已发
    st = load_state()
    posted = set(st.get("posted_urls", []))
    items = [i for i in items if i["url"] not in posted]
    items.sort(key=lambda x: -x.get("score", 0))
    return items


# ---------------- 文案 ----------------
def fill(tpl, item):
    return tpl.format(
        name=item.get("name", ""), stars=item.get("stars", 0),
        desc=(item.get("desc") or "")[:90], url=item.get("url", ""),
        title=(item.get("name") or "")[:80],
    )


def make_post(item):
    tpl = random.choice(CFG["templates"]["post"])
    text = fill(tpl, item)
    if len(text) > CFG["limits"].get("max_chars", 260):
        text = text[: CFG["limits"]["max_chars"] - 3] + "..."
    return text


def make_comment(item, post_snippet=""):
    tpl = random.choice(CFG["templates"]["comment"])
    return tpl.format(snippet=post_snippet[:40] or "这个", name=item.get("name", ""), url=item.get("url", ""))


# ---------------- 浏览器 ----------------
def launch(pw, headless=False):
    from playwright.sync_api import sync_playwright  # noqa
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(BASE / "profile"),
        headless=headless,
        viewport={"width": 1280, "height": 860},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def human_pause(a=0.6, b=1.8):
    time.sleep(random.uniform(a, b))


def is_logged_in(page):
    page.goto("https://x.com/home", wait_until="domcontentloaded")
    human_pause(2, 4)
    try:
        page.wait_for_selector('article, [data-testid="tweetTextarea_0"]', timeout=15000)
        return True
    except Exception:
        return False


def do_post(page, text):
    page.goto("https://x.com/home", wait_until="domcontentloaded")
    human_pause(2.5, 4.5)
    page.mouse.wheel(0, random.randint(150, 450))
    human_pause(1, 2)
    page.click('[data-testid="tweetTextarea_0"]', timeout=15000)
    human_pause(0.5, 1.2)
    page.keyboard.type(text, delay=random.randint(35, 85))
    human_pause(1, 2)
    page.click('[data-testid="tweetButtonInline"]', timeout=15000)
    human_pause(3, 5)
    # 回传链接:从最新一条自己帖子的链接里取
    try:
        href = page.eval_on_selector('article a[href*="/status/"]', "el => el.href")
        return href
    except Exception:
        return ""


def find_candidate_posts(page, keyword, limit=5):
    """搜索相关帖子,返回 (url, snippet) 列表。"""
    q = urllib.parse.quote(keyword)
    page.goto("https://x.com/search?q=%s&src=typed_query&f=live" % q, wait_until="domcontentloaded")
    human_pause(4, 6)
    out = []
    try:
        arts = page.query_selector_all("article")
    except Exception:
        return out
    for a in arts[: limit * 2]:
        try:
            link = a.query_selector('a[href*="/status/"]')
            text = (a.inner_text() or "").strip().replace("\n", " ")
            if link and text:
                href = link.get_attribute("href") or ""
                if href.startswith("/"):
                    href = "https://x.com" + href
                out.append((href.split("?")[0], text[:120]))
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out


def do_comment(page, url, text):
    page.goto(url, wait_until="domcontentloaded")
    human_pause(3, 5)
    page.mouse.wheel(0, random.randint(120, 320))
    human_pause(1, 2)
    page.click('[data-testid="tweetTextarea_0"]', timeout=15000)
    human_pause(0.5, 1.2)
    page.keyboard.type(text, delay=random.randint(35, 85))
    human_pause(1, 2)
    page.click('[data-testid="tweetButtonInline"]', timeout=15000)
    human_pause(3, 5)
    return True


def do_reply_mentions(page, max_replies=3):
    """回复通知里的 @我 的人。"""
    page.goto("https://x.com/notifications", wait_until="domcontentloaded")
    human_pause(4, 6)
    done = 0
    try:
        arts = page.query_selector_all("article")
    except Exception:
        return 0
    for a in arts:
        if done >= max_replies:
            break
        try:
            link = a.query_selector('a[href*="/status/"]')
            if not link:
                continue
            href = link.get_attribute("href") or ""
            if href.startswith("/"):
                href = "https://x.com" + href
            href = href.split("?")[0]
            page.goto(href, wait_until="domcontentloaded")
            human_pause(2.5, 4)
            page.click('[data-testid="tweetTextarea_0"]', timeout=12000)
            human_pause(0.5, 1)
            page.keyboard.type(random.choice(CFG["templates"]["reply"]), delay=random.randint(35, 80))
            human_pause(1, 2)
            page.click('[data-testid="tweetButtonInline"]', timeout=12000)
            human_pause(2.5, 4)
            done += 1
        except Exception as e:
            log("reply skip: %s" % str(e)[:80])
    return done


# ---------------- 调度 ----------------
def in_quiet_hours():
    h = now().hour
    q0, q1 = CFG["limits"].get("quiet_hours", [1, 7])
    return q0 <= h < q1


def can_act(st):
    lim = CFG["limits"]
    if in_quiet_hours():
        return False, "quiet hours"
    if st["hour_count"] >= lim.get("per_hour", 3):
        return False, "hour cap"
    if st["posts"] >= lim.get("daily_posts", 8):
        return False, "daily post cap"
    if st["comments"] + st["replies"] >= lim.get("daily_comments", 30):
        return False, "daily comment cap"
    return True, "ok"


def tick(page, st):
    """执行一轮:可能发帖、评论、回复。"""
    ok, why = can_act(st)
    if not ok:
        log("skip: %s" % why)
        return

    items = collect()
    if not items:
        log("no fresh items")
        return
    item = items[0]

    # 1) 发帖(如果还有额度且随机决定)
    if st["posts"] < CFG["limits"].get("daily_posts", 8) and random.random() < CFG["limits"].get("post_ratio", 0.35):
        text = make_post(item)
        fp = fingerprint(text)
        if fp in st.get("texts", []):
            log("duplicate text, skip")
        else:
            url = do_post(page, text)
            st["posts"] += 1
            st["hour_count"] += 1
            st["posted_urls"].append(item["url"])
            st["texts"].append(fp)
            save_state(st)
            log("POSTED %s | %s" % (url, text[:60]))
            return

    # 2) 回复 @我 的人
    if st["replies"] < CFG["limits"].get("daily_replies", 8) and random.random() < 0.5:
        n = do_reply_mentions(page, max_replies=1)
        if n:
            st["replies"] += n
            st["hour_count"] += n
            save_state(st)
            log("REPLIED %d" % n)
            return

    # 3) 评论相关帖
    kw = random.choice(CFG.get("sources", {}).get("search_keywords", ["AI skills"]))
    cands = find_candidate_posts(page, kw, limit=5)
    random.shuffle(cands)
    for url, snippet in cands:
        if url in st.get("commented_urls", []):
            continue
        text = make_comment(item, snippet)
        if fingerprint(text) in st.get("texts", []):
            continue
        try:
            do_comment(page, url, text)
            st["comments"] += 1
            st["hour_count"] += 1
            st["commented_urls"].append(url)
            st["texts"].append(fingerprint(text))
            save_state(st)
            log("COMMENTED %s | %s" % (url, text[:50]))
            return
        except Exception as e:
            log("comment failed %s: %s" % (url, str(e)[:60]))
    log("no comment target")


def next_sleep():
    lim = CFG["limits"]
    return random.randint(lim.get("gap_min", 20) * 60, lim.get("gap_max", 90) * 60)


# ---------------- 入口 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="首次登录")
    ap.add_argument("--dry-run", action="store_true", help="只搜集+生成文案")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--headless", action="store_true", help="无头模式")
    args = ap.parse_args()

    global CFG
    CFG = load_json(CFG_PATH, {})
    if not CFG:
        log("config.json missing")
        sys.exit(1)

    if args.dry_run:
        items = collect()
        log("dry-run: %d items" % len(items))
        for it in items[:5]:
            print("-", it["name"], "|", it["stars"], "|", it["url"])
        if items:
            print("\n--- sample post ---")
            print(make_post(items[0]))
            print("\n--- sample comment ---")
            print(make_comment(items[0]))
        return

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx, page = launch(pw, headless=args.headless)
        if args.login:
            page.goto("https://x.com/login", wait_until="domcontentloaded")
            print(">>> 在浏览器里登录 X,登录完成后回到这里按 Enter 保存登录态...")
            input()
            if is_logged_in(page):
                log("LOGIN OK")
            else:
                log("LOGIN FAILED (still not logged in)")
            ctx.close()
            return

        if not is_logged_in(page):
            log("NOT LOGGED IN — run: python x_auto.py --login")
            ctx.close()
            sys.exit(2)

        st = load_state()
        log("START (posts=%d comments=%d replies=%d)" % (st["posts"], st["comments"], st["replies"]))
        while True:
            try:
                st = load_state()
                tick(page, st)
                if args.once:
                    break
            except KeyboardInterrupt:
                log("stopped by user")
                break
            except Exception as e:
                log("tick error: %s" % str(e)[:200])
                time.sleep(60)
                continue
            s = next_sleep()
            log("sleep %d min" % (s // 60))
            time.sleep(s)
        ctx.close()


if __name__ == "__main__":
    main()
