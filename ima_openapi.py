# -*- coding: utf-8 -*-
"""ima 官方 OpenAPI 客户端（管理 + 检索 + 批量下载）。

仅覆盖官方开放能力：知识库列表、目录/文件列举、媒体直链、下载、上传。
完整问答/LLM 走 ima_cookie（见 ima_cookie.py）。
"""
import os
import json
import time
import hmac
import hashlib
import urllib.request
import urllib.error
import urllib.parse

HOST = "https://ima.qq.com/openapi"
WIKI = HOST + "/wiki/v1"          # 知识库模块
NOTE = HOST + "/note/v1"          # 笔记模块
BASE = WIKI                       # 兼容旧引用
FOLDER_MEDIA_TYPE = 99
NOTE_MEDIA_TYPE = 11

# 文件后缀 -> (media_type, content_type)
EXT_MAP = {
    "pdf": (1, "application/pdf"),
    "doc": (2, "application/msword"),
    "docx": (2, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "xls": (5, "application/vnd.ms-excel"),
    "xlsx": (5, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "ppt": (8, "application/vnd.ms-powerpoint"),
    "pptx": (8, "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    "md": (7, "text/markdown"),
    "markdown": (7, "text/markdown"),
    "txt": (13, "text/plain"),
    "text": (13, "text/plain"),
    "xmind": (14, "application/x-xmind"),
    "mp3": (15, "audio/mpeg"),
    "m4a": (15, "audio/x-m4a"),
    "wav": (15, "audio/wav"),
    "png": (3, "image/png"),
    "jpg": (3, "image/jpeg"),
    "jpeg": (3, "image/jpeg"),
    "gif": (3, "image/gif"),
    "zip": (12, "application/zip"),
}


def _ext_of(name):
    return os.path.splitext(name)[1].lstrip(".").lower()


def _cos_urlencode(value):
    # COS 签名要求：除 A-Za-z0-9 -_.~ 外全部百分号编码
    return urllib.parse.quote(str(value), safe="-_.~")


def _cos_put(local_path, cred, content_type):
    """用 COS 临时凭证（sha1 签名）上传文件，返回 (http_code, resp_text)。"""
    secret_id = cred["secret_id"]
    secret_key = cred["secret_key"]
    token = cred["token"]
    bucket = cred["bucket_name"]
    region = cred["region"]
    cos_key = cred["cos_key"]
    host = "%s.cos.%s.myqcloud.com" % (bucket, region)
    url = "https://%s/%s" % (host, cos_key)

    with open(local_path, "rb") as f:
        body = f.read()

    start = str(cred["start_time"])
    expired = str(cred["expired_time"])
    key_time = "%s;%s" % (start, expired)

    headers = {
        "host": host,
        "content-type": content_type,
        "x-cos-security-token": token,
    }
    header_keys = sorted(headers.keys())
    header_list = ";".join(header_keys)
    http_headers = "&".join(
        "%s=%s" % (k, _cos_urlencode(headers[k])) for k in header_keys
    )
    http_params = ""
    url_param_list = ""
    # URI：保留斜杠，逐段编码
    uri_path = "/" + "/".join(_cos_urlencode(p) for p in cos_key.split("/"))

    # COS 签名 v1 格式：四段以 \n 连接，且末尾仍有 \n
    http_string = "put\n%s\n%s\n%s\n" % (uri_path, http_params, http_headers)
    sign_key = hmac.new(
        secret_key.encode("utf-8"), key_time.encode("utf-8"), hashlib.sha1
    ).hexdigest()
    string_to_sign = "sha1\n%s\n%s\n" % (
        key_time,
        hashlib.sha1(http_string.encode("utf-8")).hexdigest(),
    )
    signature = hmac.new(
        sign_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1
    ).hexdigest()
    authorization = (
        "q-sign-algorithm=sha1&q-ak=%s&q-sign-time=%s&q-key-time=%s"
        "&q-header-list=%s&q-url-param-list=%s&q-signature=%s"
        % (secret_id, key_time, key_time, header_list, url_param_list, signature)
    )

    req = urllib.request.Request(url, data=body, method="PUT")
    req.add_header("Authorization", authorization)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.getcode(), r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except Exception as e:  # noqa
        return -1, str(e)


def _http_json(url, payload, headers, timeout=30, retries=3):
    data = json.dumps(payload).encode("utf-8")
    last = None
    for _ in range(retries):
        req = urllib.request.Request(url, data=data, method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                last = json.loads(e.read().decode("utf-8"))
            except Exception:
                last = {"code": e.code, "msg": str(e)}
            # 速率限制 200001 / 参数错误 110001 不重试
            if isinstance(last, dict) and last.get("code") in (200001,):
                time.sleep(2)
                continue
            return last
        except Exception as e:
            last = str(e)
            time.sleep(1)
    return {"code": -1, "msg": str(last)}


class OpenAPIClient:
    def __init__(self, client_id, api_key):
        self.client_id = client_id
        self.api_key = api_key

    def _headers(self):
        return {
            "Content-Type": "application/json",
            "ima-openapi-clientid": self.client_id,
            "ima-openapi-apikey": self.api_key,
        }

    def _call(self, path, payload, prefix=None, **kw):
        return _http_json((prefix or WIKI) + "/" + path, payload, self._headers(), **kw)

    # ---- 知识库列表 ----
    def list_knowledge_bases(self):
        """分页拉取全部可加入知识库，返回 [(id, name), ...]。"""
        out = []
        cursor = ""
        while True:
            payload = {"limit": 50}
            if cursor:
                payload["cursor"] = cursor
            res = self._call("get_addable_knowledge_base_list", payload)
            if not isinstance(res, dict) or res.get("code") not in (0, None):
                if not out:  # 首次失败才抛错
                    return res
                break
            lst = (res.get("data") or {}).get("addable_knowledge_base_list") or []
            for kb in lst:
                out.append((kb.get("id", ""), kb.get("name", "未命名")))
            data = res.get("data") or {}
            if data.get("is_end") or not data.get("next_cursor"):
                break
            cursor = data.get("next_cursor", "")
        return out

    # ---- 列举某个目录下的条目（media_type=99 为文件夹） ----
    def list_items(self, kb_id, folder_id=None, limit=50):
        payload = {"knowledge_base_id": kb_id, "limit": limit}
        if folder_id:
            payload["folder_id"] = folder_id
        return self._call("get_knowledge_list", payload)

    def iter_items(self, kb_id, folder_id=None):
        """分页迭代某目录下的所有条目。"""
        cursor = ""
        while True:
            payload = {"knowledge_base_id": kb_id, "limit": 50}
            if folder_id:
                payload["folder_id"] = folder_id
            if cursor:
                payload["cursor"] = cursor
            res = self._call("get_knowledge_list", payload)
            if not isinstance(res, dict) or "data" not in res:
                yield None, res
                break
            items = res.get("data", {}).get("knowledge_list", [])
            for it in items:
                yield it, None
            data = res.get("data", {})
            if data.get("is_end") or not data.get("next_cursor"):
                break
            cursor = data.get("next_cursor", "")

    def get_media_info(self, media_id):
        return self._call("get_media_info", {"media_id": media_id})

    # ---- 下载 ----
    def build_download_url(self, media_info):
        ui = (media_info or {}).get("data", {}).get("url_info") or {}
        url = ui.get("url")
        if not url:
            return None, None
        sep = "&" if "?" in url else "?"
        url = url + sep + "response-content-disposition=attachment"
        return url, ui.get("headers", {})

    def download_file(self, url, headers, path, timeout=180, retries=3):
        last = None
        for _ in range(retries):
            try:
                req = urllib.request.Request(url, method="GET")
                for k, v in (headers or {}).items():
                    req.add_header(k, v)
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = r.read()
                with open(path, "wb") as f:
                    f.write(data)
                return len(data)
            except Exception as e:
                last = e
                time.sleep(2)
        raise last

    # ---- 递归下载 ----
    def download_tree(self, kb_id, folder_id, out_dir, recursive=True,
                      on_log=None, on_progress=None):
        def log(msg):
            if on_log:
                on_log(msg)

        def sanitize(name):
            for c in '\\/:*?"<>|':
                name = name.replace(c, "_")
            return name[:200]

        os.makedirs(out_dir, exist_ok=True)
        total = [0]
        done = [0]

        def walk(fid, d):
            for it, err in self.iter_items(kb_id, fid):
                if err is not None:
                    log(f"[ERR] 列举失败: {err}")
                    return
                title = it.get("title", "")
                mid = it.get("media_id", "")
                mt = it.get("media_type")
                if mt == FOLDER_MEDIA_TYPE:
                    if not recursive:
                        log(f"[SKIP-DIR] {title}")
                        continue
                    log(f"[DIR] {title}")
                    walk(mid, os.path.join(d, sanitize(title)))
                else:
                    total[0] += 1
                    mi = self.get_media_info(mid)
                    url, hdrs = self.build_download_url(mi)
                    if not url:
                        log(f"[SKIP] {title} (无下载链接)")
                        continue
                    fname = sanitize(title)
                    fpath = os.path.join(d, fname)
                    try:
                        sz = self.download_file(url, hdrs, fpath)
                        done[0] += 1
                        log(f"[OK] {title}  ({sz} bytes)")
                    except Exception as e:
                        log(f"[FAIL] {title}: {e}")
                    if on_progress:
                        on_progress(done[0], total[0])

        log(f"START kb={kb_id} folder={folder_id} -> {out_dir}")
        t0 = time.time()
        walk(folder_id, out_dir)
        log(f"DONE {done[0]}/{total[0]} in {time.time()-t0:.1f}s")
        return done[0], total[0]

    # ---- 上传 ----
    def upload_file(self, kb_id, folder_id, local_path, on_log=None):
        """上传单个本地文件到知识库指定文件夹（folder_id 可空=根目录）。"""
        def log(m):
            if on_log:
                on_log(m)

        name = os.path.basename(local_path)
        ext = _ext_of(name)
        mt_ct = EXT_MAP.get(ext, (13, "application/octet-stream"))
        media_type, content_type = mt_ct
        size = os.path.getsize(local_path)
        if size == 0:
            return False, "文件为空"

        # 1) 查重（可选，失败不阻断）
        try:
            self._call("check_repeated_names",
                       {"knowledge_base_id": kb_id, "names": [name]})
        except Exception:
            pass

        # 2) 创建媒体，获取 COS 临时凭证
        cr = self._call("create_media", {
            "knowledge_base_id": kb_id,
            "file_name": name,
            "file_size": size,
            "content_type": content_type,
            "file_ext": ext or "bin",
            "media_type": media_type,
        })
        if not isinstance(cr, dict) or cr.get("code") not in (0, None):
            return False, "创建媒体失败: " + json.dumps(cr, ensure_ascii=False)[:200]
        data = cr.get("data", {})
        media_id = data.get("media_id")
        cred = data.get("cos_credential")
        if not media_id or not cred:
            return False, "缺少 media_id / cos_credential"
        log("已获取 COS 上传凭证，开始直传...")

        # 3) 直传 COS
        code, resp = _cos_put(local_path, cred, content_type)
        if code != 200:
            return False, "COS 上传失败 http=%s: %s" % (code, resp[:200])
        log("COS 上传完成，入库中...")

        # 4) 入库
        ar = self._call("add_knowledge", {
            "knowledge_base_id": kb_id,
            "media_type": media_type,
            "media_id": media_id,
            "title": name,
            "folder_id": folder_id or "",
        })
        if not isinstance(ar, dict) or ar.get("code") not in (0, None):
            return False, "入库失败: " + json.dumps(ar, ensure_ascii=False)[:200]
        log("入库成功: " + name)
        return True, name

    # ==================================================================
    # 笔记模块（openapi/note/v1，2026-09-20 实测通过）
    # ==================================================================
    def create_note(self, markdown, folder_name=None):
        """新建一篇 ima 笔记。markdown 必须是 Markdown 文本（content_format=1）。

        返回 (note_id, err)；note_id 为字符串数字，如 "7507350093048034"。
        """
        body = {"content_format": 1, "content": markdown}
        if folder_name:
            body["folder_name"] = folder_name
        res = self._call("import_doc", body, prefix=NOTE)
        if not isinstance(res, dict):
            return "", "响应异常: %s" % str(res)[:200]
        if res.get("code") not in (0, None):
            return "", "创建笔记失败: " + json.dumps(res, ensure_ascii=False)[:200]
        nid = str((res.get("data") or {}).get("note_id") or "")
        if not nid:
            return "", "未返回 note_id: " + json.dumps(res, ensure_ascii=False)[:200]
        return nid, ""

    def get_note_content(self, note_id, content_format=1):
        """读笔记正文。content_format: 0=纯文本，1=Markdown（换行与格式保留）。"""
        res = self._call("get_doc_content",
                         {"note_id": str(note_id), "target_content_format": content_format},
                         prefix=NOTE)
        if not isinstance(res, dict) or res.get("code") not in (0, None):
            raise RuntimeError("读取笔记失败: " + json.dumps(res, ensure_ascii=False)[:200])
        return (res.get("data") or {}).get("content") or ""

    def add_note_to_kb(self, kb_id, note_id, title, folder_id=None):
        """把已有笔记加入知识库。返回 (media_id, err)。"""
        body = {
            "media_type": NOTE_MEDIA_TYPE,
            "note_info": {"content_id": str(note_id)},
            "title": title,
            "knowledge_base_id": kb_id,
        }
        if folder_id:
            body["folder_id"] = folder_id
        res = self._call("add_knowledge", body)
        if not isinstance(res, dict) or res.get("code") not in (0, None):
            return "", "加入知识库失败: " + json.dumps(res, ensure_ascii=False)[:200]
        return str((res.get("data") or {}).get("media_id") or ""), ""

    def note_edit_url(self, note_id):
        """笔记在 ima 官方站点的地址（在浏览器打开）。"""
        return "https://ima.qq.com/note?note_id=%s" % str(note_id)


def note_id_from_media(media_id, media_info=None):
    """从知识库条目的 media_id 反解 note_id。

    实测格式：note_<32位hex>_<note_id>；若已取得 get_media_info，
    优先用其中的 notebook_ext_info.notebook_id。
    """
    if isinstance(media_info, dict):
        nid = (((media_info.get("data") or {}).get("notebook_ext_info") or {})
               .get("notebook_id"))
        if nid:
            return str(nid)
    s = str(media_id or "")
    if not s.startswith("note_"):
        return ""
    tail = s.rsplit("_", 1)[-1]
    return tail if tail.isdigit() else ""


# 便捷：文件名清理
def sanitize_name(name):
    for c in '\\/:*?"<>|':
        name = name.replace(c, "_")
    return name[:200]


if __name__ == "__main__":
    import os as _os
    cid = _os.environ.get("IMA_CLIENT_ID", "")
    ckey = _os.environ.get("IMA_API_KEY", "")
    c = OpenAPIClient(cid, ckey)
    print(json.dumps(c.list_knowledge_bases(), ensure_ascii=False, indent=2)[:800])
