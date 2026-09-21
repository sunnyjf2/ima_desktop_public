# -*- coding: utf-8 -*-
"""最小智能体：SQLite 记忆库 + YAML 技能库 + md 整理编排器。

生成源（llm）由调用方注入：默认是 ima Cookie 通道（见 ima_cookie.ImaCookieClient），
这样智能体无需关心后端，统一走 ask(question)->str 协议。
"""
import os
import sqlite3
import time
import json

import config

try:
    import yaml
except Exception:  # PyYAML 可能未装，做个最小兼容
    yaml = None


class MemoryDB:
    def __init__(self, path=None):
        self.path = path or config.DB_PATH
        self.conn = sqlite3.connect(self.path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS memory ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "ts TEXT, role TEXT, content TEXT)"
        )
        self.conn.commit()

    def add(self, content, role="note"):
        self.conn.execute(
            "INSERT INTO memory (ts, role, content) VALUES (?,?,?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), role, content))
        self.conn.commit()

    def recent(self, limit=50):
        cur = self.conn.execute(
            "SELECT id, ts, role, content FROM memory ORDER BY id DESC LIMIT ?",
            (limit,))
        return [
            {"id": r[0], "ts": r[1], "role": r[2], "content": r[3]}
            for r in cur.fetchall()
        ]


class SkillsManager:
    def __init__(self, path=None):
        self.dir = path or config.SKILLS_DIR
        os.makedirs(self.dir, exist_ok=True)

    def list(self):
        out = []
        for fn in os.listdir(self.dir):
            if fn.endswith(".yaml") or fn.endswith(".yml"):
                d = self._load(os.path.join(self.dir, fn))
                if d:
                    out.append(d)
        return out

    def _load(self, path):
        try:
            if yaml:
                with open(path, "r", encoding="utf-8") as f:
                    d = yaml.safe_load(f) or {}
            else:
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
            d["_file"] = os.path.basename(path)
            return d
        except Exception:
            return None

    def save(self, name, description, prompt, overwrite=False):
        safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)
        path = os.path.join(self.dir, safe + ".yaml")
        if os.path.exists(path) and not overwrite:
            return None, "已存在同名技能，请先删除或用 overwrite"
        doc = {"name": name, "description": description, "prompt": prompt}
        with open(path, "w", encoding="utf-8") as f:
            if yaml:
                yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)
            else:
                json.dump(doc, f, ensure_ascii=False, indent=2)
        return path, None

    def delete(self, filename):
        p = os.path.join(self.dir, filename)
        if os.path.exists(p):
            os.remove(p)
            return True
        return False


class MemoryFiles:
    """文件化记忆：data/memory/ 下的 .md 文件，每个文件是一份"记忆文件"。

    条目格式：  - [YYYY-MM-DD HH:MM:SS] (role) content
    支持：多文件选择/切换、导入(上传)外部文件、删除文件、删除单条记忆。
    """

    def __init__(self, dir_path=None):
        self.dir = dir_path or config.MEMORY_DIR
        os.makedirs(self.dir, exist_ok=True)

    # ---- 文件名安全化 ----
    def _safe(self, name):
        name = (name or "").strip()
        if not name.endswith(".md"):
            name += ".md"
        return "".join(c if (c.isalnum() or c in "-_. \u4e00-\u9fff") else "_"
                       for c in name)

    def path_of(self, name):
        return os.path.join(self.dir, self._safe(name))

    def list_files(self):
        try:
            return sorted(f for f in os.listdir(self.dir) if f.endswith(".md"))
        except Exception:
            return []

    def ensure_default(self):
        if not self.list_files():
            self.create("默认记忆")
        return self.list_files()[0]

    def create(self, name):
        p = self.path_of(name)
        if os.path.exists(p):
            return None, "同名记忆文件已存在"
        with open(p, "w", encoding="utf-8") as f:
            f.write("# 记忆文件：%s\n\n" % os.path.basename(p)[:-3])
        return os.path.basename(p), None

    def delete(self, name):
        p = self.path_of(name)
        if os.path.exists(p):
            os.remove(p)
            return True
        return False

    def import_file(self, src_path):
        """把外部文件导入为一份记忆文件（同名自动加序号）。"""
        if not os.path.exists(src_path):
            return None, "源文件不存在"
        base = os.path.basename(src_path)
        if not base.lower().endswith((".md", ".txt")):
            base += ".md"
        target = base
        i = 1
        while os.path.exists(self.path_of(target)):
            stem = base[:-3]
            target = "%s_%d.md" % (stem, i)
            i += 1
        try:
            with open(src_path, "rb") as f:
                data = f.read()
            with open(self.path_of(target), "wb") as f:
                f.write(data)
        except Exception as e:
            return None, "导入失败: %s" % e
        return os.path.basename(self.path_of(target)), None

    def read_text(self, name):
        p = self.path_of(name)
        if not os.path.exists(p):
            return ""
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

    def entries(self, name):
        """把文件解析成条目列表 [{line_index, ts, role, content}]。"""
        out = []
        for idx, line in enumerate(self.read_text(name).splitlines()):
            s = line.strip()
            if not s.startswith("- ["):
                continue
            try:
                ts_end = s.index("]", 3)
                ts = s[3:ts_end]
                rest = s[ts_end + 1:].strip()
                role = "note"
                if rest.startswith("("):
                    r_end = rest.index(")")
                    role = rest[1:r_end]
                    rest = rest[r_end + 1:].strip()
                out.append({"line_index": idx, "ts": ts, "role": role,
                            "content": rest})
            except Exception:
                continue
        return out

    def append(self, name, content, role="note"):
        content = (content or "").replace("\n", " ").strip()
        if not content:
            return
        p = self.path_of(name)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(p, "a", encoding="utf-8") as f:
            f.write("- [%s] (%s) %s\n" % (ts, role, content))

    def delete_entry(self, name, index):
        """按 entries() 返回的序号删除第 index 条。"""
        p = self.path_of(name)
        if not os.path.exists(p):
            return False
        lines = self.read_text(name).splitlines()
        ents = self.entries(name)
        if index < 0 or index >= len(ents):
            return False
        del lines[ents[index]["line_index"]]
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return True

    def recent_text(self, name, limit=10):
        ents = self.entries(name)
        ents = ents[-limit:]
        return "\n".join("[%s] %s" % (e["role"], e["content"]) for e in ents)


class Agent:
    """接收 llm 函数（question->str），负责把任务编排成最终文本。"""

    def __init__(self, llm, memory=None, skills=None):
        self.llm = llm
        self.memory = memory or MemoryDB()
        self.skills = skills or SkillsManager()

    def _skills_text(self):
        items = self.skills.list()
        if not items:
            return "（暂无技能）"
        return "\n".join("- %s: %s" % (i.get("name"), i.get("description", ""))
                         for i in items)

    def organize_markdown(self, md_path, instruction=None, skill=None):
        if not os.path.exists(md_path):
            return None, "文件不存在"
        with open(md_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        skill_prompt = ""
        if skill:
            skill_prompt = ("\n请遵循以下技能要求：\n" + skill.get("prompt", ""))
        instr = instruction or "请通读全文，提炼核心结构、修正明显错乱、输出整理后的 Markdown。"
        prompt = (
            "你是一个资料整理助手。下面是一段从知识库下载的 Markdown 资料。\n"
            "目标：%s%s\n\n"
            "==== 原始资料 ====\n%s\n\n==== 请输出整理后的完整 Markdown ===="
            % (instr, skill_prompt, text[:20000])
        )
        result = self.llm(prompt)
        self.memory.add("整理文件: %s" % os.path.basename(md_path), role="task")
        return result, None

    def chat(self, message, use_memory=True):
        if use_memory:
            mem = self.memory.recent(10)
            ctx = "\n".join("[%s] %s" % (m["role"], m["content"]) for m in mem)
            prompt = ("参考以下记忆上下文回答用户问题：\n%s\n\n用户：%s" % (ctx, message))
        else:
            prompt = message
        return self.llm(prompt)


if __name__ == "__main__":
    m = MemoryDB()
    m.add("测试记忆")
    print("recent:", m.recent(3))
    s = SkillsManager()
    p, e = s.save("示例技能", "整理八字笔记", "按流派分段，保留原文术语。")
    print("skill saved:", p, e)
    print("skills:", [x["name"] for x in s.list()])
