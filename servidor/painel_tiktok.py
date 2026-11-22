#!/usr/bin/env python3
"""
Painel de Cortes - servidor local do TikTok
-------------------------------------------
Roda no seu PC e faz a ponte entre o site
https://gabrielgc20001-debug.github.io/painel-de-cortes/ e a API do TikTok.

- Guarda o Client Key / Client Secret (so no seu PC, em config.json)
- Troca o "code" do login do TikTok por um token
- Lista os cortes (.mp4/.mov) da pasta de cortes
- Envia o video pro TikTok (publicacao direta ou rascunho)

Uso:  python painel_tiktok.py
So usa a biblioteca padrao do Python 3.8+ (nada para instalar).
"""
import json
import mimetypes
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
TOKEN_PATH = os.path.join(HERE, "tiktok_token.json")
PORT = 8787

SITE = "https://gabrielgc20001-debug.github.io"
REDIRECT_URI = SITE + "/painel-de-cortes/callback.html"
ALLOWED_ORIGINS = {SITE, "http://localhost:8000", "http://127.0.0.1:8000"}
SCOPES = "user.info.basic,video.publish,video.upload"
API = "https://open.tiktokapis.com"
VIDEO_EXT = (".mp4", ".mov", ".webm")

STATE = {"oauth_states": set(), "jobs": {}}


# ---------------------------------------------------------------- config
def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    changed = False
    if not cfg.get("client_key"):
        print("\nPrimeira vez? Copie do TikTok for Developers > Painel de Cortes > Sandbox > Credentials")
        cfg["client_key"] = input("Cole o Client key: ").strip()
        changed = True
    if not cfg.get("client_secret"):
        cfg["client_secret"] = input("Cole o Client secret: ").strip()
        changed = True
    if not cfg.get("clips_dir"):
        default = os.path.join(HERE, "cortes")
        d = input(f"Pasta dos cortes [{default}]: ").strip().strip('"') or default
        cfg["clips_dir"] = d
        changed = True
    os.makedirs(cfg["clips_dir"], exist_ok=True)
    if changed:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    return cfg


CFG = None


def load_token():
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_token(tok):
    tok["saved_at"] = int(time.time())
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        json.dump(tok, f, indent=2)


# ---------------------------------------------------------------- http helpers
def http(method, url, data=None, headers=None, raw=None):
    headers = dict(headers or {})
    body = None
    if raw is not None:
        body = raw
    elif data is not None:
        if headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
            body = urllib.parse.urlencode(data).encode()
        else:
            headers.setdefault("Content-Type", "application/json; charset=UTF-8")
            body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            txt = r.read().decode() or "{}"
            return r.status, json.loads(txt) if txt.strip().startswith("{") else {"raw": txt}
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, {"raw": txt}


def access_token():
    tok = load_token()
    if not tok:
        raise RuntimeError("TikTok nao conectado")
    # renova se faltar menos de 5 min
    if time.time() > tok.get("saved_at", 0) + tok.get("expires_in", 0) - 300:
        st, new = http("POST", API + "/v2/oauth/token/", {
            "client_key": CFG["client_key"], "client_secret": CFG["client_secret"],
            "grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
        }, {"Content-Type": "application/x-www-form-urlencoded"})
        if "access_token" not in new:
            raise RuntimeError("Falha ao renovar token: %s" % new)
        save_token(new)
        tok = new
    return tok["access_token"]


def tiktok(method, path, data=None):
    return http(method, API + path, data, {"Authorization": "Bearer " + access_token()})


# ---------------------------------------------------------------- upload job
def run_upload(job_id, path, mode, post_info):
    job = STATE["jobs"][job_id]
    try:
        size = os.path.getsize(path)
        MB = 1024 * 1024
        if size <= 64 * MB:
            chunk, count = size, 1
        else:
            chunk = 10 * MB
            count = size // chunk  # o ultimo chunk leva o resto
        source = {"source": "FILE_UPLOAD", "video_size": size,
                  "chunk_size": chunk, "total_chunk_count": count}
        if mode == "direct":
            st, res = tiktok("POST", "/v2/post/publish/video/init/",
                             {"post_info": post_info, "source_info": source})
        else:
            st, res = tiktok("POST", "/v2/post/publish/inbox/video/init/",
                             {"source_info": source})
        if res.get("error", {}).get("code") not in (None, "ok"):
            raise RuntimeError(res["error"].get("message") or res["error"]["code"])
        publish_id = res["data"]["publish_id"]
        upload_url = res["data"]["upload_url"]
        job.update(publish_id=publish_id, status="ENVIANDO")
        ctype = mimetypes.guess_type(path)[0] or "video/mp4"
        with open(path, "rb") as f:
            for i in range(count):
                start = i * chunk
                end = size - 1 if i == count - 1 else start + chunk - 1
                f.seek(start)
                part = f.read(end - start + 1)
                st, r = http("PUT", upload_url, raw=part, headers={
                    "Content-Type": ctype,
                    "Content-Length": str(len(part)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                })
                if st not in (200, 201, 206):
                    raise RuntimeError(f"Upload falhou ({st}): {r}")
                job["progress"] = round((end + 1) / size * 100)
        # acompanha o processamento no TikTok
        for _ in range(120):
            st, s = tiktok("POST", "/v2/post/publish/status/fetch/", {"publish_id": publish_id})
            status = (s.get("data") or {}).get("status", "?")
            job["status"] = status
            if status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX", "FAILED"):
                if status == "FAILED":
                    job["error"] = (s.get("data") or {}).get("fail_reason")
                break
            time.sleep(3)
        job["done"] = True
    except Exception as e:  # noqa
        job.update(status="ERRO", error=str(e), done=True)


# ---------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("  ", self.command, self.path.split("?")[0], "->", args[1] if len(args) > 1 else "")

    def cors(self):
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    def clip_path(self, name):
        name = os.path.basename(urllib.parse.unquote(name))
        p = os.path.join(CFG["clips_dir"], name)
        if not os.path.isfile(p) or not name.lower().endswith(VIDEO_EXT):
            raise FileNotFoundError(name)
        return p

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/api/ping":
                return self.send_json({"ok": True, "connected": load_token() is not None})
            if u.path == "/api/auth-url":
                state = secrets.token_urlsafe(16)
                STATE["oauth_states"].add(state)
                q = urllib.parse.urlencode({
                    "client_key": CFG["client_key"], "scope": SCOPES, "response_type": "code",
                    "redirect_uri": REDIRECT_URI, "state": state})
                return self.send_json({"url": "https://www.tiktok.com/v2/auth/authorize/?" + q})
            if u.path == "/api/me":
                st, me = tiktok("GET", "/v2/user/info/?fields=open_id,avatar_url,display_name")
                st2, ci = tiktok("POST", "/v2/post/publish/creator_info/query/", {})
                return self.send_json({"user": (me.get("data") or {}).get("user"),
                                       "creator": ci.get("data"), "error": ci.get("error")})
            if u.path == "/api/clips":
                files = sorted((f for f in os.listdir(CFG["clips_dir"]) if f.lower().endswith(VIDEO_EXT)),
                               key=lambda f: -os.path.getmtime(os.path.join(CFG["clips_dir"], f)))
                return self.send_json({"clips": [{"name": f, "mb": round(os.path.getsize(
                    os.path.join(CFG["clips_dir"], f)) / 1048576, 1)} for f in files]})
            if u.path.startswith("/api/clip/"):
                p = self.clip_path(u.path[len("/api/clip/"):])
                size = os.path.getsize(p)
                rng = self.headers.get("Range")
                start, end = 0, size - 1
                if rng and rng.startswith("bytes="):
                    a, _, b = rng[6:].partition("-")
                    start = int(a or 0)
                    end = int(b) if b else min(size - 1, start + 4 * 1048576)
                self.send_response(206 if rng else 200)
                self.cors()
                self.send_header("Content-Type", mimetypes.guess_type(p)[0] or "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                if rng:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                with open(p, "rb") as f:
                    f.seek(start)
                    self.wfile.write(f.read(end - start + 1))
                return
            if u.path.startswith("/api/job/"):
                return self.send_json(STATE["jobs"].get(u.path.rsplit("/", 1)[-1], {"error": "job?"}))
            self.send_json({"error": "not found"}, 404)
        except FileNotFoundError:
            self.send_json({"error": "arquivo nao encontrado"}, 404)
        except Exception as e:  # noqa
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            b = self.body()
            if u.path == "/api/exchange":
                if b.get("state") not in STATE["oauth_states"]:
                    return self.send_json({"error": "state invalido, clique em Conectar de novo"}, 400)
                STATE["oauth_states"].discard(b["state"])
                st, tok = http("POST", API + "/v2/oauth/token/", {
                    "client_key": CFG["client_key"], "client_secret": CFG["client_secret"],
                    "code": b.get("code"), "grant_type": "authorization_code",
                    "redirect_uri": REDIRECT_URI,
                }, {"Content-Type": "application/x-www-form-urlencoded"})
                if "access_token" not in tok:
                    return self.send_json({"error": tok}, 400)
                save_token(tok)
                return self.send_json({"ok": True, "scope": tok.get("scope")})
            if u.path == "/api/logout":
                tok = load_token()
                if tok:
                    http("POST", API + "/v2/oauth/revoke/", {
                        "client_key": CFG["client_key"], "client_secret": CFG["client_secret"],
                        "token": tok["access_token"]}, {"Content-Type": "application/x-www-form-urlencoded"})
                    os.remove(TOKEN_PATH)
                return self.send_json({"ok": True})
            if u.path == "/api/publish":
                path = self.clip_path(b["clip"])
                mode = b.get("mode", "direct")
                post_info = {
                    "title": b.get("title", "")[:2200],
                    "privacy_level": b.get("privacy_level"),
                    "disable_comment": not b.get("allow_comment", False),
                    "disable_duet": not b.get("allow_duet", False),
                    "disable_stitch": not b.get("allow_stitch", False),
                    "brand_content_toggle": bool(b.get("brand_content")),
                    "brand_organic_toggle": bool(b.get("brand_organic")),
                    "video_cover_timestamp_ms": 1000,
                }
                if mode == "direct" and not post_info["privacy_level"]:
                    return self.send_json({"error": "escolha a privacidade"}, 400)
                job_id = secrets.token_hex(6)
                STATE["jobs"][job_id] = {"id": job_id, "clip": b["clip"], "mode": mode,
                                         "status": "INICIANDO", "progress": 0, "done": False}
                threading.Thread(target=run_upload, args=(job_id, path, mode, post_info), daemon=True).start()
                return self.send_json({"job": job_id})
            self.send_json({"error": "not found"}, 404)
        except FileNotFoundError:
            self.send_json({"error": "arquivo nao encontrado"}, 404)
        except Exception as e:  # noqa
            self.send_json({"error": str(e)}, 500)


if __name__ == "__main__":
    CFG = load_config()
    print(f"\nPainel de Cortes - servidor TikTok rodando em http://localhost:{PORT}")
    print(f"Pasta dos cortes: {CFG['clips_dir']}")
    print(f"Abra: {SITE}/painel-de-cortes/   (deixe esta janela aberta)\n")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
