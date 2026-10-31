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
import os
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
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def acquire_lock():
    """跨平台单实例锁:拿不到锁返回 None。"""
    lock_path = BASE / ".lock"
    f = open(lock_path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except Exception:
        f.close()
        return None


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


# ---------------- LLM 文案 ----------------
STYLE_SYSTEM = """你是我(中文 X 用户)的文案助理。输出必须像真人在手机上随手打的:
- 短句、口语,可用语气词(啊/哈/吧/嘛/真的/讲真/笑死/有点东西)
- 禁用 AI 腔:首先/其次/综上/总之/值得注意的是/作为一个/希望对你有帮助
- 不排比、不堆 emoji、不喊口号、不自我介绍
- 链接放最后,不加"链接在下面"
- 只输出文案本身,不要解释、不加引号"""


def api_key():
    """key 只从环境变量或 .env 读，永不进仓库。"""
    cfg = CFG.get("llm", {})
    key = os.environ.get(cfg.get("api_key_env", "LLM_API_KEY"), "")
    if not key:
        env_file = BASE / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("LLM_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"')
                    break
    return key


def llm_chat(user_prompt, timeout=60):
    cfg = CFG.get("llm", {})
    if not cfg.get("enabled", False):
        return None
    key = api_key()
    if not key:
        return None
    models = [cfg.get("model", "deepseek-chat")] + list(cfg.get("fallback_models", []))
    extra = cfg.get("headers", {}) or {}
    for model in models:
        body = {
            "model": model,
            "messages": [{"role": "system", "content": STYLE_SYSTEM},
                         {"role": "user", "content": user_prompt}],
            "temperature": cfg.get("temperature", 0.95),
            "max_tokens": cfg.get("max_tokens", 300),
        }
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
        headers.update(extra)
        req = urllib.request.Request(
            cfg.get("base_url", "https://api.deepseek.com/v1").rstrip("/") + "/chat/completions",
            data=json.dumps(body).encode(), method="POST", headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode())
            text = (d["choices"][0]["message"]["content"] or "").strip()
            if text:
                return text
        except Exception as e:
            log("llm %s err: %s" % (model, str(e)[:100]))
            time.sleep(1.5)
            continue
    return None


def gen_post(item):
    prompt = ("素材:GitHub 仓库 %s,star %s,简介:%s,链接:%s\n"
              "写一条中文推文:一句洞察 + 仓库名和 star + 链接 + 一个具体问题,不超过 240 字符。口语。" % (
                  item.get("name"), item.get("stars"), (item.get("desc") or "")[:100], item.get("url")))
    t = llm_chat(prompt)
    return t.strip() if t else make_post(item)


def gen_comment(item, snippet):
    prompt = ("原帖内容:%s\n可提到的仓库:%s(star %s,链接 %s)\n"
              "写一条 1-2 句中文评论:先接对方的话,再补一个信息点或经验;不吹捧;链接可选,放最后。不超过 100 字。" % (
                  (snippet or "")[:150], item.get("name"), item.get("stars"), item.get("url")))
    t = llm_chat(prompt)
    return t.strip() if t else make_comment(item, snippet)


def gen_reply(snippet):
    prompt = "有人回复我:%s\n写一句 1-2 句中文回复,口语,像真人聊天,不超过 60 字。" % (snippet or "")[:150]
    t = llm_chat(prompt)
    return t.strip() if t else random.choice(CFG["templates"]["reply"])


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


LOGIN_SELECTORS = (
    '[data-testid="SideNav_AccountSwitcher_Button"]'
    ', [data-testid="tweetTextarea_0"]'
    ', [data-testid="primaryColumn"] article'
)

SEND_SELECTORS = ('[data-testid="tweetButtonInline"]', '[data-testid="tweetButton"]')


def dismiss_overlays(page):
    """关掉 cookie 条 / 弹层：点不到发送键最常见的原因就是它挡着。"""
    for sel in ('[data-testid="BottomBar"] [role="button"]',
                'div[role="button"]:has-text("Accept all cookies")',
                '[data-testid="sheetDialog"] [role="button"]:has-text("Close")'):
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=2500)
                human_pause(0.4, 0.9)
        except Exception:
            continue


def is_logged_in(page):
    page.goto("https://x.com/home", wait_until="domcontentloaded")
    human_pause(2.5, 4)
    dismiss_overlays(page)
    try:
        page.wait_for_selector(LOGIN_SELECTORS, timeout=15000)
        return True
    except Exception:
        # 兜底：没被踢回登录页，且侧栏在 —— 空时间线/慢加载时不至于误判掉登录
        return ("/login" not in page.url
                and page.query_selector('[data-testid="AppTabBar_Home_Link"], nav[aria-label]') is not None)


def click_send(page, timeout_ms=12000):
    """发送键 testid 会在 tweetButtonInline / tweetButton 之间换版，两个都试，最后 Ctrl+Enter 兜底。"""
    for sel in SEND_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible() and el.is_enabled():
                el.click(timeout=timeout_ms)
                return True, sel
        except Exception:
            continue
    try:
        page.keyboard.press("Control+Enter")
        return True, "ctrl+enter"
    except Exception as e:
        return False, str(e)[:80]


def posted_evidence(page, timeout_s=15):
    """发送成功的硬证据：输入框清空，或出现 toast。只靠 click 不报错不算发出去。"""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            el = page.query_selector('[data-testid="tweetTextarea_0"]')
            if el is None or not (el.inner_text() or "").strip():
                return True, "composer cleared"
        except Exception:
            return True, "composer gone"
        try:
            toast = page.query_selector('[data-testid="toast"]')
            if toast:
                t = (toast.inner_text() or "").strip()
                if t:
                    return True, "toast: %s" % t[:60]
        except Exception:
            pass
        time.sleep(0.6)
    return False, "no evidence (composer still has text)"


def do_post(page, text):
    """发一条。返回 (ok, url, detail)。"""
    try:
        page.goto("https://x.com/home", wait_until="domcontentloaded")
        human_pause(2.5, 4.5)
        dismiss_overlays(page)
        page.mouse.wheel(0, random.randint(150, 450))
        human_pause(1, 2)
        page.click('[data-testid="tweetTextarea_0"]', timeout=15000)
        human_pause(0.5, 1.2)
        page.keyboard.type(text, delay=random.randint(35, 85))
        human_pause(1, 2)
        clicked, how = click_send(page)
        if not clicked:
            return False, "", "send button not found: %s" % how
        human_pause(2, 3)
        ok, ev = posted_evidence(page)
        if not ok:
            return False, "", "%s (clicked %s)" % (ev, how)
        # 回传链接:从最新一条自己帖子的链接里取
        url = ""
        try:
            url = page.eval_on_selector('article a[href*="/status/"]', "el => el.href")
        except Exception:
            pass
        return True, url, "%s via %s" % (ev, how)
    except Exception as e:
        return False, "", str(e)[:120]


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
    """评论一条。返回 (ok, detail)。"""
    try:
        page.goto(url, wait_until="domcontentloaded")
        human_pause(3, 5)
        dismiss_overlays(page)
        page.mouse.wheel(0, random.randint(120, 320))
        human_pause(1, 2)
        page.click('[data-testid="tweetTextarea_0"]', timeout=15000)
        human_pause(0.5, 1.2)
        page.keyboard.type(text, delay=random.randint(35, 85))
        human_pause(1, 2)
        clicked, how = click_send(page)
        if not clicked:
            return False, "send button not found: %s" % how
        human_pause(2, 3)
        ok, ev = posted_evidence(page)
        if not ok:
            return False, "%s (clicked %s)" % (ev, how)
        return True, "%s via %s" % (ev, how)
    except Exception as e:
        return False, str(e)[:120]


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
            snippet = (a.inner_text() or "")[:200]
            page.keyboard.type(gen_reply(snippet), delay=random.randint(35, 80))
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
        text = gen_post(item)
        fp = fingerprint(text)
        if fp in st.get("texts", []):
            log("duplicate text, skip")
        else:
            ok, url, why = do_post(page, text)
            if not ok:
                log("post failed: %s" % why)
                return
            st["posts"] += 1
            st["hour_count"] += 1
            st["posted_urls"].append(item["url"])
            st["texts"].append(fp)
            save_state(st)
            log("POSTED %s | %s | %s" % (url, text[:60], why))
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
        text = gen_comment(item, snippet)
        if fingerprint(text) in st.get("texts", []):
            continue
        ok, why = do_comment(page, url, text)
        if not ok:
            log("comment failed %s: %s" % (url, why))
            continue
        st["comments"] += 1
        st["hour_count"] += 1
        st["commented_urls"].append(url)
        st["texts"].append(fingerprint(text))
        save_state(st)
        log("COMMENTED %s | %s | %s" % (url, text[:50], why))
        return
    log("no comment target")


def next_sleep():
    lim = CFG["limits"]
    return random.randint(lim.get("gap_min", 20) * 60, lim.get("gap_max", 90) * 60)


def run_test(page):
    """强制自检:发 1 帖 + 评论 1 条(忽略静默与限额)。最后一行 TEST RESULT 是给运维 agent 读的。"""
    st = load_state()
    log("TEST START")
    items = collect()
    if not items:
        log("TEST RESULT: FAIL — 素材为空(查网络/GitHub 限流)")
        return 1
    item = items[0]
    text = gen_post(item)
    if not text.strip():
        log("TEST RESULT: FAIL — 文案为空(查 LLM key/额度)")
        return 1

    ok, url, why = do_post(page, text)
    if not ok:
        log("TEST POST FAILED: %s" % why)
        log("TEST RESULT: FAIL — 发帖未成功,计数未加")
        return 1
    st["posts"] += 1
    st["hour_count"] += 1
    st["posted_urls"].append(item["url"])
    st["texts"].append(fingerprint(text))
    save_state(st)
    log("TEST POSTED %s | %s | %s" % (url, text[:60], why))

    kw = random.choice(CFG.get("sources", {}).get("search_keywords", ["AI skills"]))
    cands = find_candidate_posts(page, kw, limit=3)
    log("test: %d comment candidates (%s)" % (len(cands), kw))
    for u, snip in cands:
        if u in st.get("commented_urls", []):
            continue
        ctext = gen_comment(item, snip)
        ok, why2 = do_comment(page, u, ctext)
        if not ok:
            log("TEST COMMENT FAILED %s: %s" % (u, why2))
            continue
        st["comments"] += 1
        st["hour_count"] += 1
        st["commented_urls"].append(u)
        st["texts"].append(fingerprint(ctext))
        save_state(st)
        log("TEST COMMENTED %s | %s | %s" % (u, ctext[:50], why2))
        log("TEST RESULT: PASS")
        return 0
    log("TEST RESULT: PARTIAL — 发帖成功,评论无可用目标(搜索可能被限流,不阻断)")
    return 0


# ---------------- 自检(不碰 X) ----------------
def run_check():
    """安装自检:配置 / key / 素材源 / LLM / 锁 / 状态。不打开浏览器、不发任何东西。"""
    ok_all = True

    def line(ok, msg):
        nonlocal ok_all
        ok_all = ok_all and ok
        print(("[ok  ] " if ok else "[FAIL] ") + msg)

    print("== x_auto self check ==")
    lim = CFG.get("limits", {})
    line(bool(CFG), "config.json 已加载 limits.daily_posts=%s quiet=%s" % (
        lim.get("daily_posts"), lim.get("quiet_hours")))
    for f in ("x_auto.py", "config.json", "requirements.txt"):
        line((BASE / f).exists(), "%s 存在" % f)
    line(bool(api_key()), "LLM_API_KEY 已配置(.env 或环境变量)")
    try:
        items = collect()
        line(len(items) > 0, "素材搜集 %d 条" % len(items))
    except Exception as e:
        line(False, "素材搜集失败: %s" % str(e)[:80])
    t = llm_chat("用一句中文口语说你会写文案。")
    line(bool(t), "LLM 出文案: %s" % ((t or "")[:40] or "空"))
    lock = acquire_lock()
    line(lock is not None, "单实例锁可获取(无残留进程)")
    if lock:
        try:
            lock.close()
        except Exception:
            pass
    st = load_state()
    print("   状态: day=%s posts=%s comments=%s replies=%s hour_count=%s texts=%s" % (
        st.get("day"), st.get("posts"), st.get("comments"), st.get("replies"),
        st.get("hour_count"), len(st.get("texts", []))))
    print("RESULT: %s" % ("PASS" if ok_all else "FAIL"))
    return 0 if ok_all else 1


# ---------------- 入口 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="首次登录")
    ap.add_argument("--dry-run", action="store_true", help="只搜集+生成文案")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--headless", action="store_true", help="无头模式")
    ap.add_argument("--test", action="store_true", help="强制测试:发 1 帖 + 评论 1 条(忽略静默/限额)")
    ap.add_argument("--check", action="store_true", help="安装自检(不打开浏览器、不发布)")
    args = ap.parse_args()

    global CFG
    CFG = load_json(CFG_PATH, {})
    if not CFG:
        log("config.json missing")
        sys.exit(1)

    if args.check:
        sys.exit(run_check())

    lock = acquire_lock()
    if not lock:
        log("another instance is running (lock held) — exit")
        return

    if args.dry_run:
        items = collect()
        log("dry-run: %d items" % len(items))
        for it in items[:5]:
            print("-", it["name"], "|", it["stars"], "|", it["url"])
        if items:
            print("\n--- sample post (LLM if configured) ---")
            print(gen_post(items[0]))
            print("\n--- sample comment (LLM if configured) ---")
            print(gen_comment(items[0], "这个确实"))
        return

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx, page = launch(pw, headless=args.headless)
        try:
            if args.login:
                page.goto("https://x.com/login", wait_until="domcontentloaded")
                print(">>> 在浏览器里登录 X(最多等 5 分钟,登录成功后自动继续)...")
                ok = False
                for _ in range(30):
                    time.sleep(10)
                    if is_logged_in(page):
                        ok = True
                        break
                log("LOGIN OK" if ok else "LOGIN TIMEOUT")
                return 0 if ok else 3

            if not is_logged_in(page):
                log("NOT LOGGED IN — run: python3 x_auto.py --login")
                return 2

            if args.test:
                return run_test(page)

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
            return 0
        finally:
            try:
                ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
