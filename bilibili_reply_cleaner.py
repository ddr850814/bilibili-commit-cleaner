# -*- coding: utf-8 -*-
"""
B站批量删除自己发过的回复/评论（扫码登录 + 可靠验证版）

使用方法：
    1. (首次) pip install requests qrcode pillow
    2. python bilibili_reply_cleaner.py
    3. 手机B站App扫码登录
    4. 预览 -> 输入 yes 确认 -> 自动删除并验证

核心机制：
    - 数据源1: aicu.cc 拉历史评论（覆盖面广，但有延迟）
    - 数据源2: 消息中心(msgfeed)拉被赞/被回复的评论（覆盖最新，oid更准确）
    - 两个数据源按 rpid 去重合并，msgfeed 的 oid 优先
    - 删除后用 reply/reply 接口验证：
        code=12022(已被删除) = 删除成功
        code=0(有数据) = 评论仍在 = 删除失败
        code=12006(没有该评论) = 评论本就不存在
    - oid 无效(视频已删)的评论会自动跳过
    - 失败不重试，直接记录到 delete_failed.json

注意：aicu.cc 数据非实时，最近几天的评论可能查不到。
"""

import os
import sys
import time
import json
import urllib.parse

try:
    import requests
except ImportError:
    print("缺少依赖 requests，请先运行：pip install requests")
    sys.exit(1)

# ===================== 配置区 =====================
DELETE_INTERVAL = 3.0       # 删除间隔(秒)
VERIFY_DELAY = 1.5          # 删除后等待多久再验证(秒)
AICU_PAGE_SIZE = 500
KEYWORD_FILTER = []         # 只删含这些关键字的评论，空=全删
SAVE_LIST_TO_FILE = True
LIST_FILENAME = "my_bilibili_replies.json"
CONFIRM_BEFORE_DELETE = True
# =================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}

QR_GEN_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate?source=main-fe-header"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?source=main-fe-header&qrcode_key={key}"
AICU_API = "https://api.aicu.cc/api/v3/search/getreply?uid={uid}&pn={pn}&ps={ps}&mode=0&keyword="
DELETE_API = "https://api.bilibili.com/x/v2/reply/del"
# 验证接口：code=0存在 / 12022已删除 / 12006不存在
CHECK_API = "https://api.bilibili.com/x/v2/reply/reply?oid={oid}&root={rpid}&type=1&pn=1&ps=1"
# 消息中心接口（补充 aicu 覆盖不到的最近评论）
MSGFEED_LIKE_API = "https://api.bilibili.com/x/msgfeed/like?platform=web&build=0&mobi_app=web"
MSGFEED_REPLY_API = "https://api.bilibili.com/x/msgfeed/reply?platform=web&build=0&mobi_app=web"

# 从 native_uri 提取视频 oid 的正则
import re
_VIDEO_URI_RE = re.compile(r"bilibili://video/(\d+)")


# ---------------------- 扫码登录 ----------------------
def login_by_qrcode(log=print, qr_callback=None):
    try:
        import qrcode
    except ImportError:
        raise RuntimeError("缺少依赖 qrcode，请先运行：pip install qrcode pillow")

    log("正在获取二维码 ...")
    resp = requests.get(QR_GEN_URL, headers=HEADERS, timeout=10).json()
    if resp.get("code") != 0:
        raise RuntimeError(f"获取二维码失败：{resp}")

    qr_url = resp["data"]["url"]
    qrcode_key = resp["data"]["qrcode_key"]

    img = qrcode.make(qr_url)

    # GUI 模式：通过回调显示二维码；CLI 模式：保存并打开图片
    if qr_callback:
        qr_callback(img)
    else:
        qr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_login_qr.png")
        img.save(qr_path)
        try:
            os.startfile(qr_path)
        except Exception:
            log(f"请手动打开二维码图片：{qr_path}")

    log("请用【手机B站App】扫描二维码完成登录 ...")

    sess = requests.Session()
    sess.headers.update(HEADERS)

    while True:
        r = sess.get(QR_POLL_URL.format(key=qrcode_key), timeout=10).json()
        code = r.get("data", {}).get("code")
        if code == 0:
            cross_url = r["data"]["url"]
            params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(cross_url).query))
            sess.cookies.set("SESSDATA", params.get("SESSDATA", ""), domain=".bilibili.com")
            sess.cookies.set("bili_jct", params.get("bili_jct", ""), domain=".bilibili.com")
            sess.cookies.set("DedeUserID", params.get("DedeUserID", ""), domain=".bilibili.com")
            log("扫码登录成功！\n")
            return sess, params.get("DedeUserID", ""), params.get("bili_jct", "")
        elif code == 86090:
            log("已扫描，请在手机上点击确认 ...")
        elif code == 86101:
            pass  # 等待扫码，不刷屏
        elif code == 86038:
            raise RuntimeError("二维码已过期，请重新运行。")
        else:
            log(f"未知状态：{r}")
        time.sleep(2)


# ---------------------- 拉取历史评论 ----------------------
def fetch_my_replies(session, uid, log=print):
    log(f"开始从 aicu.cc 拉取 UID={uid} 的历史评论 ...")
    results = []
    page = 1
    total = None
    while True:
        url = AICU_API.format(uid=uid, pn=page, ps=AICU_PAGE_SIZE)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            body = resp.json()
        except Exception as e:
            log(f"  第 {page} 页请求失败：{e}，3秒后重试 ...")
            time.sleep(3)
            continue

        data = body.get("data")
        if not data:
            log(f"  aicu 返回异常：{body}")
            break

        cursor = data.get("cursor", {})
        if total is None:
            total = cursor.get("all_count", 0)
            log(f"  历史评论总数（aicu 记录）：{total}")

        replies = data.get("replies", []) or []
        for item in replies:
            try:
                parent = item.get("parent") or {}
                results.append({
                    "rpid": int(item["rpid"]),
                    "oid": int(item["dyn"]["oid"]),
                    "type": int(item["dyn"]["type"]),
                    "message": item.get("message", ""),
                    "rootid": int(parent["rootid"]) if parent.get("rootid") else None,
                })
            except (KeyError, ValueError, TypeError):
                continue

        log(f"  第 {page} 页 {len(replies)} 条，累计 {len(results)} 条")
        if cursor.get("is_end") or not replies:
            break
        page += 1
        time.sleep(1)

    log(f"拉取完成，共 {len(results)} 条\n")
    return results


def apply_filter(replies, log=print):
    if not KEYWORD_FILTER:
        return replies
    kept = [r for r in replies if any(k in r["message"] for k in KEYWORD_FILTER)]
    log(f"关键字过滤：{len(replies)} -> {len(kept)} 条\n")
    return kept


# ---------------------- 消息中心数据源（补充最新评论）----------------------
def _parse_oid_from_uri(uri, native_uri, business_id):
    """
    从消息中心的 uri/native_uri 解析 oid 和 type。
    逻辑参考 Initsnow/bilibili-comment-cleaning 的 parse_oid 函数。
    返回 (oid, type) 或 (None, None)
    """
    uri = uri or ""
    native_uri = native_uri or ""
    if "t.bilibili.com" in uri:
        oid = uri.replace("https://t.bilibili.com/", "").strip("/")
        try:
            tp = business_id if business_id else 17
            return int(oid), tp
        except ValueError:
            return None, None
    elif "h.bilibili.com/ywh/" in uri:
        oid = uri.replace("https://h.bilibili.com/ywh/", "").strip("/")
        try:
            return int(oid), 11
        except ValueError:
            return None, None
    elif "www.bilibili.com/read/cv" in uri:
        oid = uri.split("/read/cv")[-1].split("?")[0].strip("/")
        try:
            return int(oid), 12
        except ValueError:
            return None, None
    elif "www.bilibili.com/video/" in uri or "www.bilibili.com/bangumi/play/" in uri:
        m = _VIDEO_URI_RE.search(native_uri)
        if m:
            return int(m.group(1)), 1
    return None, None


def fetch_from_msgfeed(session, log=print):
    """
    从消息中心拉取"被点赞"和"被回复"的评论，补充 aicu 覆盖不到的最近评论。
    消息中心返回的 native_uri 包含准确的 oid。
    返回 {rpid: {rpid, oid, type, message, rootid}} 字典
    """
    result = {}
    sources = [
        ("被点赞的评论", MSGFEED_LIKE_API, "like_time"),
        ("被回复的评论", MSGFEED_REPLY_API, "reply_time"),
    ]

    for label, base_url, time_field in sources:
        log(f"从消息中心拉取{label} ...")
        cursor_id = None
        cursor_time = None
        count = 0

        while True:
            params = {"platform": "web", "build": 0, "mobi_app": "web"}
            if cursor_id is not None and cursor_time is not None:
                params["id"] = cursor_id
                params[time_field] = cursor_time

            try:
                resp = session.get(base_url, params=params, timeout=15)
                body = resp.json()
            except Exception as e:
                log(f"  请求失败：{e}，跳过此数据源")
                break

            if body.get("code") != 0:
                log(f"  接口返回错误：code={body.get('code')} msg={body.get('message')}")
                break

            data = body.get("data") or {}

            # like 接口结构: data.total.cursor / data.total.items
            # reply 接口结构: data.cursor / data.items
            if "total" in data:
                total_data = data["total"]
                cursor = total_data.get("cursor")
                items = total_data.get("items", []) or []
            else:
                cursor = data.get("cursor")
                items = data.get("items", []) or []

            for it in items:
                item = it.get("item") or {}
                # like: rpid = item_id; reply: rpid = target_id
                rpid = item.get("item_id") or item.get("target_id")
                if not rpid:
                    continue

                uri = item.get("uri", "")
                native_uri = item.get("native_uri", "")
                business_id = item.get("business_id", 0)
                oid, type_ = _parse_oid_from_uri(uri, native_uri, business_id)

                if oid is None:
                    continue

                # root_id 在 native_uri 的 comment_root_id 参数里
                rootid = None
                if "comment_root_id=" in native_uri:
                    try:
                        rootid = int(native_uri.split("comment_root_id=")[1].split("&")[0])
                    except (ValueError, IndexError):
                        pass

                # source_content / target_reply_content 是我的评论内容
                msg = (item.get("source_content") or
                       item.get("target_reply_content") or
                       item.get("title") or "")

                result[int(rpid)] = {
                    "rpid": int(rpid),
                    "oid": oid,
                    "type": type_,
                    "message": msg,
                    "rootid": rootid,
                }
                count += 1

            # 更新游标
            if cursor:
                cursor_id = cursor.get("id")
                cursor_time = cursor.get("time")
                if cursor.get("is_end"):
                    break
            else:
                break

            time.sleep(0.5)

        log(f"  {label}：获取 {count} 条")

    return result


# ---------------------- 验证 + 删除 ----------------------
def _get_json_with_retry(session, url, retries=2):
    """带重试的 GET 请求，接口不稳定时重试"""
    for attempt in range(retries + 1):
        try:
            body = session.get(url, timeout=10).json()
            # 排除风控/限流错误码，这些需要重试
            code = body.get("code", 0)
            if code in (-352, -509, -412, -799):
                if attempt < retries:
                    time.sleep(2)
                    continue
            return body
        except Exception:
            if attempt < retries:
                time.sleep(2)
                continue
            return {"code": -1, "message": "请求异常"}
    return {"code": -1, "message": "重试用完"}


def check_reply_status(session, oid, rpid, rootid=None):
    """
    用 reply/reply 接口查询单条评论状态。
    对于根评论(rootid=None)：直接查 root=rpid，code=12022=已删，code=0=存在
    对于子回复(rootid有值)：查 root=rootid 的子回复列表，搜索 rpid
    返回: 'exists' / 'deleted' / 'missing' / 'error'
    """
    # 根评论：rootid 为空或等于自身 rpid（数据源常把根评论的 rootid 设为自身）
    if not rootid or rootid == rpid:
        url = CHECK_API.format(oid=oid, rpid=rpid)
        body = _get_json_with_retry(session, url)
        code = body.get("code")
        if code == 0:
            return "exists"
        elif code == 12022:
            return "deleted"
        elif code == 12006:
            return "missing"
        else:
            return "error"
    else:
        # 子回复：查父评论(rootid)下的子回复列表，搜索 rpid
        for pn in range(1, 11):  # 最多翻10页
            url = f"https://api.bilibili.com/x/v2/reply/reply?oid={oid}&root={rootid}&type=1&pn={pn}&ps=49"
            body = _get_json_with_retry(session, url)
            code = body.get("code")
            if code == 12022:
                return "deleted"    # 父评论已被删 -> 子回复也删了
            if code == 12006:
                return "missing"
            if code != 0:
                return "error"
            replies = (body.get("data") or {}).get("replies") or []
            if not replies:
                # 翻完了都没找到 -> 子回复已被删
                return "deleted"
            for rp in replies:
                if rp.get("rpid") == rpid:
                    return "exists"  # 在子回复列表中找到了 -> 仍存在
            # 继续翻页
        # 翻完10页都没找到
        return "deleted"


def delete_reply(session, bili_jct, oid, type_, rpid, rootid=None):
    data = {"oid": oid, "type": type_, "rpid": rpid, "csrf": bili_jct}
    # 子回复需要额外的 root 参数（父评论 rpid）
    if rootid and rootid != rpid:
        data["root"] = rootid
    resp = session.post(DELETE_API, data=data, timeout=10)
    try:
        return resp.json()
    except Exception:
        return {"code": -1, "message": "返回非JSON"}


def delete_and_verify(session, bili_jct, oid, type_, rpid, rootid=None):
    """
    删除并验证。
    返回 (status, detail)
      status: "CONFIRMED" 已确认删除 / "ALREADY_DELETED" 本就已删
              "DELETE_FAILED" 删除后仍存在 / "OID_INVALID" oid无效(视频已删) / "ERROR"
    """
    # 先检查评论当前状态
    status_before = check_reply_status(session, oid, rpid, rootid)
    if status_before == "deleted":
        return "ALREADY_DELETED", "评论本就处于已删除状态"
    if status_before == "missing":
        return "OID_INVALID", "评论不存在(oid无效或评论已消失)"
    # status_before == 'exists' 或 'error'：都继续尝试删除
    # （验证接口不稳定时返回 error，不应跳过删除）

    # status_before == 'exists' 或 'error'，执行删除
    body = delete_reply(session, bili_jct, oid, type_, rpid, rootid)
    if body.get("code") != 0:
        return "DELETE_FAILED", f"删除接口报错 code={body.get('code')} msg={body.get('message','')}"

    # 等待后验证
    time.sleep(VERIFY_DELAY)
    status_after = check_reply_status(session, oid, rpid, rootid)

    if status_after == "deleted":
        return "CONFIRMED", "删除并验证通过"
    elif status_after == "exists":
        return "DELETE_FAILED", "删除请求成功但评论仍存在"
    elif status_after == "missing":
        return "CONFIRMED", "删除后评论已消失"
    else:
        # 删除前后验证都返回 error：oid 可能失效（视频已删/评论区关闭）
        # 删除接口幂等（已删评论也返回 code:0），无法判断是否本次生效
        return "UNVERIFIED", "删除请求成功但无法验证(oid可能失效)"


def preview(replies, log=print):
    log("=" * 60)
    log(f"即将处理 {len(replies)} 条评论，前 20 条预览：")
    log("=" * 60)
    for i, r in enumerate(replies[:20], 1):
        msg = r["message"].replace("\n", " ")
        if len(msg) > 60:
            msg = msg[:60] + "..."
        log(f"  [{i:2d}] rpid={r['rpid']} oid={r['oid']}")
        log(f"       {msg}")
    if len(replies) > 20:
        log(f"  ... 还有 {len(replies) - 20} 条未显示")
    log("=" * 60)


# ---------------------- 主流程 ----------------------
def run_pipeline(log=print, confirm_func=None, progress_func=None, qr_callback=None, auto_confirm=False):
    """
    运行完整的删除流程。可通过回调参数适配 CLI / GUI。
      log:          日志输出函数
      confirm_func: 确认删除的回调，返回 bool。None=用 CLI input
      progress_func: 进度回调 progress_func(idx, total, stats_dict)
      qr_callback:   二维码图片回调 qr_callback(pil_image)
      auto_confirm:  跳过确认
    """
    log("== B站历史回复批量清理工具（可靠验证版） ==\n")

    session, uid, bili_jct = login_by_qrcode(log=log, qr_callback=qr_callback)

    # 1. 从 aicu.cc 拉取历史评论
    replies = fetch_my_replies(session, uid, log=log)

    # 2. 从消息中心拉取最近评论（补充 aicu 覆盖不到的）
    msgfeed_replies = fetch_from_msgfeed(session, log=log)

    # 3. 合并：按 rpid 去重，msgfeed 的 oid 更准确，优先采用
    aicu_count = len(replies)
    merged = {}
    for r in replies:
        merged[r["rpid"]] = r
    msgfeed_new = 0
    for rpid, r in msgfeed_replies.items():
        if rpid in merged:
            merged[rpid]["oid"] = r["oid"]
            merged[rpid]["type"] = r["type"]
            if r.get("rootid") and not merged[rpid].get("rootid"):
                merged[rpid]["rootid"] = r["rootid"]
        else:
            merged[rpid] = r
            msgfeed_new += 1
    replies = list(merged.values())

    log(f"\n合并完成：aicu {aicu_count} 条 + 消息中心新增 {msgfeed_new} 条 = 共 {len(replies)} 条\n")

    if not replies:
        log("没有拉到任何评论，结束。")
        return

    replies = apply_filter(replies, log=log)

    if SAVE_LIST_TO_FILE:
        with open(LIST_FILENAME, "w", encoding="utf-8") as f:
            json.dump(replies, f, ensure_ascii=False, indent=2)
        log(f"评论列表已保存到 {LIST_FILENAME}\n")

    preview(replies, log=log)

    # 确认删除
    if not auto_confirm:
        if confirm_func:
            if not confirm_func(len(replies)):
                log("已取消。")
                return
        else:
            if CONFIRM_BEFORE_DELETE:
                ans = input("\n确认删除以上全部评论？输入 yes 继续，其它任意键退出：").strip()
                if ans.lower() != "yes":
                    print("已取消。")
                    return

    # 开始删除+验证
    confirmed = []
    already_deleted = []
    failed = []
    oid_invalid = []
    unverified = []
    total = len(replies)

    log(f"\n开始删除并验证 {total} 条评论 ...\n")
    for idx, r in enumerate(replies, 1):
        status, detail = delete_and_verify(
            session, bili_jct, r["oid"], r["type"], r["rpid"], r.get("rootid")
        )
        r["_status"] = status
        r["_detail"] = detail

        marks = {
            "CONFIRMED": "✓已删除",
            "ALREADY_DELETED": "~本就已删",
            "DELETE_FAILED": "✗删除失败",
            "OID_INVALID": "○视频已删",
            "UNVERIFIED": "?未验证",
        }
        mark = marks.get(status, status)
        log(f"[{idx}/{total}] rpid={r['rpid']} -> {mark}  ({detail})")

        if status == "CONFIRMED":
            confirmed.append(r)
        elif status == "ALREADY_DELETED":
            already_deleted.append(r)
        elif status == "OID_INVALID":
            oid_invalid.append(r)
        elif status == "UNVERIFIED":
            unverified.append(r)
        else:
            failed.append(r)

        # 进度回调（每条都回调，GUI 自行决定刷新频率）
        if progress_func:
            progress_func(idx, total, {
                "confirmed": len(confirmed),
                "already_deleted": len(already_deleted),
                "oid_invalid": len(oid_invalid),
                "unverified": len(unverified),
                "failed": len(failed),
            })

        if idx % 10 == 0:
            log(f"  --- 进度 {idx}/{total} | 已删 {len(confirmed)} | 已删过 {len(already_deleted)} | 视频没了 {len(oid_invalid)} | 未验证 {len(unverified)} | 失败 {len(failed)} ---")

        if status in ("CONFIRMED", "UNVERIFIED"):
            time.sleep(DELETE_INTERVAL)

    # 汇总
    log("\n" + "=" * 60)
    log("删除验证汇总")
    log("=" * 60)
    log(f"  总数:             {total}")
    log(f"  ✓ 本次删除成功:   {len(confirmed)}")
    log(f"  ~ 本就已删除:     {len(already_deleted)}")
    log(f"  ? 无法验证(已请求删除): {len(unverified)}")
    log(f"  ○ 视频已删(跳过): {len(oid_invalid)}")
    log(f"  ✗ 删除失败:       {len(failed)}")
    log("=" * 60)

    if failed:
        with open("delete_failed.json", "w", encoding="utf-8") as f:
            json.dump(failed, f, ensure_ascii=False, indent=2)
        log(f"\n失败的 {len(failed)} 条已保存到 delete_failed.json")
    if oid_invalid:
        with open("oid_invalid.json", "w", encoding="utf-8") as f:
            json.dump(oid_invalid, f, ensure_ascii=False, indent=2)
        log(f"视频已删无法处理的 {len(oid_invalid)} 条已保存到 oid_invalid.json")

    log("\n完成。")


def main():
    auto = "--yes" in sys.argv
    run_pipeline(auto_confirm=auto)


if __name__ == "__main__":
    main()
