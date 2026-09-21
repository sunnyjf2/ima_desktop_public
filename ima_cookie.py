# -*- coding: utf-8 -*-
"""ima 非官方 Cookie 通道客户端（个人自用）。

链路：init_session(kb) -> assistant/qa(SSE)，token 直接取 IMA-TOKEN。
验证通过的请求构造（对齐 tencent-ima-copilot-mcp）。
用途：获得 ima 原生 LLM 能力（知识库 RAG 问答 + 自由对话）。
"""
import os
import re
import json
import time
import uuid
import base64
import random
import string
import urllib.parse
import urllib.request
import urllib.error

BASE = "https://ima.qq.com"
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0")

# 知识库内部接口（knowledge_tab_reader）。
# 注意：内部接口的知识库 id 与 OpenAPI 的 id 不是同一套（内部=纯数字/16位hex，
# OpenAPI=44位 base64），只有 media_id 通用（如 folder_7505631065936683）。
FOLDER_MEDIA_TYPE = 99
NS_READER = "knowledge_tab_reader"

# ima 官方模型表（来自 /cgi-bin/model_manage/get_models）。
# 每个模型含「快速(0)/深度思考(1)」两个子模式；mode 键 0=Instruct, 1=Thinking。
FALLBACK_MODELS = [
    {"model_name": "DeepSeek-V4-Flash", "model_id": "official_3", "model_type": 3,
     "sub_models": [("快速", "official_3", 3), ("深度思考", "official_1", 1)]},
    {"model_name": "Hy3", "model_id": "official_0", "model_type": 0,
     "sub_models": [("快速", "official_0", 0), ("深度思考", "official_2", 2)]},
    {"model_name": "Hy4 preview", "model_id": "official_1001", "model_type": 1001,
     "sub_models": [("快速", "official_1001", 1001), ("深度思考", "official_1002", 1002)]},
    {"model_name": "GLM-5.3-Flash", "model_id": "official_3000", "model_type": 3000,
     "sub_models": [("快速", "official_3000", 3000), ("深度思考", "official_3001", 3001)]},
]


def ck_get(xcookie, key):
    for part in (xcookie or "").split(";"):
        part = part.strip()
        if part.startswith(key + "="):
            return part.split("=", 1)[1].strip()
    return ""


def extract_ua(xcookie):
    iua = ck_get(xcookie, "IMA-IUA")
    return iua if iua else DEFAULT_UA


# ---------------- 鉴权续期（IMA-TOKEN 有效期约 2 小时） ----------------
# ima 的 IMA-TOKEN 是短期票据，过期后所有 cgi-bin 接口返回 600001。
# cookie 里同时带 IMA-REFRESH-TOKEN，可调 /cgi-bin/auth_login/refresh 换新票，
# 换票后 x-ima-bkn 必须用「新 token」重算，否则 41。
LOGIN_EXPIRED_CODES = (600001, 600002, 600003, 41, 110031)
LOGIN_EXPIRED_WORDS = ("登录过期", "登录失败", "请重新登录", "token expired",
                       "session expired", "会话已过期", "unauthorized")


def _i32(x):
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x >= 0x80000000 else x


def compute_bkn(token):
    """腾讯 bkn = DJBHash(token) & 0x7fffffff（32 位有符号滚动）。"""
    h = 5381
    for ch in (token or ""):
        h = _i32(h * 33 + ord(ch))
    return str(h & 0x7FFFFFFF)


def parse_uid(xcookie):
    """从 cookie 解析 IMA-UID（个人知识库 id 与它同值）。"""
    v = ck_get(xcookie, "IMA-UID")
    if v:
        return v
    m = re.search(r"user_id=([a-f0-9]{16})", xcookie or "")
    return m.group(1) if m else ""


def parse_refresh_token(xcookie):
    v = ck_get(xcookie, "IMA-REFRESH-TOKEN")
    if v:
        return urllib.parse.unquote(v)
    v = ck_get(xcookie, "IMA-TOKEN")
    return urllib.parse.unquote(v) if v else ""


def is_login_expired(obj):
    """判断响应体是否属于「登录过期」族错误。"""
    if not isinstance(obj, dict):
        return False
    if obj.get("code") in LOGIN_EXPIRED_CODES:
        return True
    msg = str(obj.get("msg") or obj.get("message") or "").lower()
    return any(w in msg for w in LOGIN_EXPIRED_WORDS)


# 进程内票据缓存：uid -> {token, bkn, xcookie, ts, ttl}
# 界面里对话页/知识库页/模型列表会各建一个客户端实例，若无缓存，
# 每个新实例首次请求都会换一次票（官方前端约 2 小时才刷一次），
# 既浪费往返也可能触发风控。这里让同账号实例共享同一张有效票据。
_TOKEN_CACHE = {}


def _cache_get(xcookie):
    ent = _TOKEN_CACHE.get(parse_uid(xcookie))
    if not ent:
        return None
    if time.time() > ent["ts"] + ent["ttl"] - 300:
        return None
    return ent


def _cache_put(uid, token, bkn, xcookie, ttl):
    if uid:
        _TOKEN_CACHE[uid] = {"token": token, "bkn": bkn, "xcookie": xcookie,
                             "ts": time.time(), "ttl": ttl}


def gen_traceparent():
    tid = "".join(random.choices("0123456789abcdef", k=32))
    sid = "".join(random.choices("0123456789abcdef", k=16))
    return "00-%s-%s-01" % (tid, sid)


def build_headers(xcookie, bkn, token=None, for_qa=False):
    if for_qa:
        accept, ctype = "*/*", "text/event-stream"
    else:
        accept, ctype = "application/json", "application/json"
    h = {
        "accept": accept,
        "accept-language": "zh-CN,zh;q=0.9",
        "content-type": ctype,
        "extension_version": "999.999.999",
        "from_browser_ima": "1",
        "priority": "u=1, i",
        "sec-ch-ua": '"Not(A:Brand";v="8", "Chromium";v="144", "Microsoft Edge";v="144"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "traceparent": gen_traceparent(),
        "x-ima-bkn": bkn,
        "x-ima-cookie": xcookie,
        "referer": "https://ima.qq.com/wikis",
        "user-agent": extract_ua(xcookie),
    }
    if for_qa:
        h["cache-control"] = "no-cache"
    if token:
        h["authorization"] = "Bearer " + token
    return h


def _post(url, body, headers, timeout=60):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


class ImaCookieClient:
    def __init__(self, xcookie, bkn, kb_id=None, model_id="official_3",
                 model_type=3, enable_enhancement=True, on_token_refreshed=None):
        self.xcookie = xcookie
        self.bkn = bkn
        self.kb_id = kb_id or ""
        self.token = ck_get(xcookie, "IMA-TOKEN")
        self.session_id = ""
        self.model_id = model_id or ""
        self.model_type = model_type if model_type is not None else 3
        self.enable_enhancement = enable_enhancement
        # 续期持久化回调：fn(new_xcookie, new_bkn) —— 由界面注入，把新票据写回配置。
        self.on_token_refreshed = on_token_refreshed
        self._token_ts = 0.0          # 上次成功取票的时间（0 = 未知，来自 cookie）
        self._token_ttl = 7200        # 服务端返回的 token_valid_time
        self.last_error = ""
        # 同账号已有新鲜票据则直接复用，避免每个新实例都换票
        ent = _cache_get(xcookie)
        if ent:
            self.token = ent["token"]
            self.bkn = ent["bkn"]
            self.xcookie = ent["xcookie"]
            self._token_ts = ent["ts"]
            self._token_ttl = ent["ttl"]

    # ---- 鉴权 ----
    def refresh(self):
        """用 IMA-REFRESH-TOKEN 换新的 IMA-TOKEN。成功返回 True。

        成功后同步更新 self.token / self.bkn / self.xcookie，并触发持久化回调。
        """
        uid = parse_uid(self.xcookie)
        rt = parse_refresh_token(self.xcookie)
        if not uid or not rt:
            self.last_error = "cookie 缺少 IMA-UID / IMA-REFRESH-TOKEN，无法自动续期"
            return False
        h = {
            "x-ima-cookie": self.xcookie,
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": extract_ua(self.xcookie),
            "referer": "https://ima.qq.com/",
            "origin": "https://ima.qq.com",
        }
        if self.bkn:
            h["x-ima-bkn"] = self.bkn
        try:
            res = _post(BASE + "/cgi-bin/auth_login/refresh",
                        {"user_id": uid, "refresh_token": rt, "token_type": 14},
                        h, timeout=30)
            j = json.loads(res)
        except Exception as e:
            self.last_error = "续期请求异常: %s" % e
            return False
        if j.get("code") != 0 or not j.get("token"):
            self.last_error = "续期失败: %s" % json.dumps(j, ensure_ascii=False)[:200]
            return False

        newtok = j["token"]
        self.token = newtok
        self.bkn = compute_bkn(newtok)          # ★ 换票后必须重算 bkn
        if re.search(r"IMA-TOKEN=[^;]*", self.xcookie or ""):
            self.xcookie = re.sub(r"IMA-TOKEN=[^;]*", "IMA-TOKEN=" + newtok, self.xcookie)
        else:
            self.xcookie = (self.xcookie or "") + "; IMA-TOKEN=" + newtok
        self._token_ts = time.time()
        self._token_ttl = int(j.get("token_valid_time") or 7200)
        self.last_error = ""
        _cache_put(parse_uid(self.xcookie), newtok, self.bkn,
                   self.xcookie, self._token_ttl)
        if self.on_token_refreshed:
            try:
                self.on_token_refreshed(self.xcookie, self.bkn)
            except Exception:
                pass
        return True

    def _token_stale(self):
        """票据是否可能已过期（留 5 分钟余量）。"""
        if not self._token_ts:
            return True                      # 启动时来自 cookie，签发时间未知
        return time.time() > self._token_ts + self._token_ttl - 300

    def _adopt_cache(self):
        """若进程内缓存里有比自己更新的票据，直接采纳，避免重复换票。"""
        ent = _TOKEN_CACHE.get(parse_uid(self.xcookie))
        if ent and ent["ts"] > self._token_ts:
            self.token = ent["token"]
            self.bkn = ent["bkn"]
            self.xcookie = ent["xcookie"]
            self._token_ts = ent["ts"]
            self._token_ttl = ent["ttl"]
            return True
        return False

    def _post_json(self, url, body, timeout=25, retry=True):
        """统一 JSON 请求：检测登录过期 -> 自动续期 -> 重试一次。"""
        self._adopt_cache()
        h = build_headers(self.xcookie, self.bkn, token=self.token, for_qa=False)
        res = _post(url, body, h, timeout=timeout)
        try:
            j = json.loads(res)
        except Exception as e:
            raise RuntimeError("非 JSON 响应: " + res[:300]) from e
        if is_login_expired(j) and retry:
            if self.refresh():
                h2 = build_headers(self.xcookie, self.bkn, token=self.token, for_qa=False)
                try:
                    j = json.loads(_post(url, body, h2, timeout=timeout))
                except Exception as e:
                    raise RuntimeError("续期后重试失败: %s" % e) from e
            elif is_login_expired(j):
                raise RuntimeError(
                    "登录已过期，自动续期失败（code=%s）。%s\n"
                    "请在「设置」中重新粘贴 x-ima-cookie / x-ima-bkn。"
                    % (j.get("code"), self.last_error))
        return j

    # ---- 模型列表（内部接口） ----
    def get_models(self):
        """返回 [{model_name, model_id, model_type, sub_models:[(label,id,type)]}]。

        登录过期会抛异常（不静默降级），让界面能提示用户。
        """
        data = self._post_json(BASE + "/cgi-bin/model_manage/get_models", {}, timeout=20)
        if data.get("code") != 0 or not isinstance(data.get("models"), list):
            raise RuntimeError("获取模型列表失败: %s"
                               % json.dumps(data, ensure_ascii=False)[:200])
        out = []
        for m in data["models"]:
            subs = []
            # 0=快速(Instruct) 1=深度思考(Thinking)
            info = m.get("sub_model_infos") or {}
            for key, label in (("0", "快速"), ("1", "深度思考")):
                sm = info.get(key)
                if sm:
                    subs.append((label, sm.get("model_id"), sm.get("model_type")))
            if not subs:
                subs = [("快速", m.get("model_id"), m.get("model_type"))]
            out.append({
                "model_name": m.get("model_name") or m.get("model_id"),
                "model_id": m.get("model_id"),
                "model_type": m.get("model_type"),
                "sub_models": subs,
            })
        return out or FALLBACK_MODELS

    def init_session(self):
        body = {
            "envInfo": {"robotType": 5, "interactType": 0},
            "relatedUrl": self.kb_id or "",
            "sceneType": 1,
            "msgsLimit": 10,
            "forbidAutoAddToHistoryList": False,
            "knowledgeBaseInfoWithFolder": {
                "knowledgeBaseId": self.kb_id or "",
                "folderIds": [],
            },
        }
        j = self._post_json(BASE + "/cgi-bin/session_logic/init_session", body)
        if j.get("code") == 0 and j.get("session_id"):
            self.session_id = j["session_id"]
            return self.session_id
        raise RuntimeError("init_session 失败: "
                           + json.dumps(j, ensure_ascii=False)[:400])

    @staticmethod
    def _extract_text(m):
        d = m.get("Data")
        if isinstance(d, dict):
            tm = d.get("text_message")
            if isinstance(tm, dict) and isinstance(tm.get("Text"), str):
                return tm["Text"]
        if isinstance(m.get("content"), str) and m["content"]:
            return m["content"]
        if isinstance(m.get("Text"), str) and m["Text"]:
            return m["Text"]
        if isinstance(m.get("answer"), str) and m["answer"]:
            return m["answer"]
        if "msgs" in m and isinstance(m["msgs"], list):
            for x in m["msgs"]:
                if isinstance(x, dict):
                    c = x.get("content")
                    if isinstance(c, str) and c:
                        return c
                    if isinstance(c, dict) and isinstance(c.get("answer"), str):
                        try:
                            inner = json.loads(c["answer"])
                            if isinstance(inner.get("Text"), str):
                                return inner["Text"]
                        except Exception:
                            return c["answer"]
        return None

    def _model_info(self):
        """按前端 buildModelInfo 的规则构造：有 model_id 则带上，否则只发 model_type。"""
        if self.model_id:
            return {"model_id": self.model_id, "model_type": self.model_type,
                    "enable_enhancement": self.enable_enhancement}
        return {"model_type": self.model_type,
                "enable_enhancement": self.enable_enhancement}

    def ask(self, question, on_chunk=None, timeout=300, _retried=False):
        """发送问题，流式返回文本。on_chunk(str) 可在每个分片到达时回调。"""
        self._adopt_cache()
        if self._token_stale():
            self.refresh()                 # 主动续期，避免 QA 直接撞 600001
        if not self.session_id:
            self.init_session()
        uskey = base64.b64encode(bytes(random.getrandbits(8) for _ in range(32))).decode()
        guid = ck_get(self.xcookie, "IMA-GUID") or "default_guid"
        body = {
            "session_id": self.session_id,
            "robot_type": 5,
            "question": question,
            "question_type": 2,
            "client_id": str(uuid.uuid4()),
            "command_info": {
                "type": 14,
                "knowledge_qa_info": {
                    "tags": [],
                    "knowledge_ids": [self.kb_id] if self.kb_id else [],
                    "media_id_infos": [],
                },
            },
            "model_info": self._model_info(),
            "history_info": {},
            "device_info": {
                "uskey": uskey,
                "uskey_bus_infos_input": (guid + "_" + str(int(time.time())))[:64],
            },
            "client_tools": [],
        }
        h = build_headers(self.xcookie, self.bkn, token=self.token, for_qa=True)
        req = urllib.request.Request(BASE + "/cgi-bin/assistant/qa",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers=h, method="POST")
        chunks = []
        stray = []          # 非 SSE 行（服务端出错时返回的是 JSON 而非事件流）
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                if not line.startswith("data:"):
                    if len(stray) < 20:
                        stray.append(line)
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    m = json.loads(payload)
                except Exception:
                    continue
                t = self._extract_text(m)
                if t:
                    chunks.append(t)
                    if on_chunk:
                        on_chunk(t)

        if not chunks and stray:
            blob = "".join(stray)
            try:
                j = json.loads(blob)
            except Exception:
                j = None
            if isinstance(j, dict) and is_login_expired(j) and not _retried:
                if self.refresh():
                    return self.ask(question, on_chunk=on_chunk,
                                    timeout=timeout, _retried=True)
                raise RuntimeError(
                    "登录已过期，自动续期失败（code=%s）。%s\n"
                    "请在「设置」中重新粘贴 x-ima-cookie / x-ima-bkn。"
                    % (j.get("code"), self.last_error))
            raise RuntimeError("问答未返回内容: " + blob[:300])
        return "".join(chunks)

    # ==================================================================
    # 知识库管理（内部接口，已实测可用）
    # ==================================================================
    def _kb_post(self, ep, body, timeout=25):
        """内部只读接口统一入口，返回解析后的 JSON dict（含自动续期）。"""
        return self._post_json(BASE + "/cgi-bin/%s/%s" % (NS_READER, ep), body,
                               timeout=timeout)

    def kb_home(self):
        """列出我的知识库 -> [{'id','name','type'}]（type 1=个人 2=已加入 3=公开）。"""
        j = self._kb_post("get_home_page_data", {})
        if j.get("code") != 0:
            raise RuntimeError("获取知识库列表失败: " + json.dumps(j, ensure_ascii=False)[:200])
        out = []
        for r in (j.get("results") or []):
            for kb in (r.get("knowledge_base_list") or []):
                out.append({
                    "id": kb.get("id") or "",
                    "name": (kb.get("basic_info") or {}).get("name") or "未命名",
                    "type": kb.get("type"),
                })
        return out

    def kb_list(self, kb_id, folder_id="", limit=50):
        """列举某目录下的全部条目（自动翻页）-> [{'media_id','title','media_type','is_folder'}]。"""
        out, cursor = [], ""
        while True:
            j = self._kb_post("get_knowledge_list", {
                "knowledge_base_id": kb_id,
                "folder_id": folder_id or "",
                "cursor": cursor,
                "limit": limit,
            })
            if j.get("code") != 0:
                raise RuntimeError("列举失败: " + json.dumps(j, ensure_ascii=False)[:200])
            for it in (j.get("knowledge_list") or []):
                out.append({
                    "media_id": it.get("media_id") or "",
                    "title": it.get("title") or "",
                    "media_type": it.get("media_type"),
                    "is_folder": it.get("media_type") == FOLDER_MEDIA_TYPE,
                })
            if j.get("is_end") or not j.get("next_cursor"):
                break
            cursor = j.get("next_cursor") or ""
        return out

    def kb_folders(self, kb_id, folder_id=""):
        """只列文件夹 -> [{'folder_id','name','file_number','folder_number'}]。"""
        j = self._kb_post("get_folder_list", {
            "knowledge_base_id": kb_id,
            "folder_id": folder_id or "",
            "cursor": "",
            "limit": 50,
        })
        if j.get("code") != 0:
            raise RuntimeError("列举文件夹失败: " + json.dumps(j, ensure_ascii=False)[:200])
        return [{"folder_id": f.get("folder_id") or "",
                 "name": f.get("name") or "",
                 "file_number": f.get("file_number"),
                 "folder_number": f.get("folder_number")}
                for f in (j.get("folder_list") or [])]



if __name__ == "__main__":
    import sys
    print("ImaCookieClient 模块：请在 UI 或脚本中调用。")
