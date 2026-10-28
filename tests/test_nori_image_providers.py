# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Image providers (2026-10-28): pictures were OpenRouter only; the image_provider setting now picks OpenRouter, OpenAI (API key) or xAI (API key).
Real SQLite and providers table; only the HTTP layer (urlopen) is faked, so what is pinned is the request each provider is sent and how its reply and
its errors are read.

Pinned: which providers can draw and which cannot; the setting falls back to OpenRouter when the chosen provider is gone; OpenRouter's call is unchanged
(the existing tests that patch chat.openrouter_image still hold); OpenAI gets /generations for a prompt and multipart /edits with image[] for a reference;
xAI gets /generations or /edits with a data-URL image, asks for base64, retries once without response_format if refused, and accepts a URL reply; errors,
timeouts, refusals and oversized replies come back as the same plain reasons the image tools already turn into messages; choosing a provider resets a model
name that only meant something at the old one; the Settings -> Images and avatar-builder forms use all of it."""
import base64
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_imgprov_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_imgprov_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"
os.environ.pop("OPENROUTER_API_KEY", None)
for _f in ("persona.default.md", "persona.template.md"):
    shutil.copy(ROOT / "prompts" / _f, os.environ["NORI_PROMPTS_DIR"])

import accounts  # noqa: E402
import avatar_builder as ab  # noqa: E402
import chat  # noqa: E402
import config  # noqa: E402
import imagegen  # noqa: E402
import providers  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

store.init()
_admin = accounts.bootstrap_admin("Ben", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]
ADMIN = {"user_id": UID, "workspace_id": WS, "role": "admin", "csrf": "csrf-tok"}


def png(color=(120, 80, 60)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (32, 40), color).save(buf, "PNG")
    return buf.getvalue()


PNG = png()
B64 = base64.b64encode(PNG).decode()


def disconnect():
    store.write(lambda c: c.execute("UPDATE models SET provider_id=NULL"))
    store.write(lambda c: c.execute("DELETE FROM providers"))


def add(type_, label=None, key="sk-test"):
    ok, _ = providers.create_api_key(WS, type_, label or type_, key, UID)
    assert ok
    return [p for p in providers.list_all(WS) if p["label"] == (label or type_)][-1]["id"]


class Resp:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self.body if n < 0 else self.body[:n]


class Net:
    """Fake urlopen: answers each call from a queue (a dict becomes JSON, an exception is raised, bytes are returned raw) and records the requests."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, req, timeout=None):
        url = req if isinstance(req, str) else req.get_full_url()
        self.requests.append({"url": url, "req": req})
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return Resp(r if isinstance(r, bytes) else json.dumps(r).encode())

    @property
    def last(self):
        return self.requests[-1]

    def header(self, i, name):
        return self.requests[i]["req"].get_header(name)

    def json(self, i=-1):
        return json.loads(self.requests[i]["req"].data.decode())


def http_error(code, body):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body.encode()))


class Base(unittest.TestCase):
    def setUp(self):
        disconnect()
        for k, v in (("image_provider", 0), ("image_model", chat.IMAGE_DEFAULT_MODELS["openrouter"]), ("image_gen_enabled", True),
                     ("image_content_precheck_enabled", True)):
            config.set("workspace", WS, k, v)


class Backend(Base):
    def test_which_provider_types_can_draw(self):
        self.assertEqual(set(chat.IMAGE_PROVIDER_TYPES), {"openrouter", "openai_api_key", "xai_api_key"})
        for t in ("anthropic_oauth", "openai_oauth", "github_copilot", "xai_oauth"):
            self.assertNotIn(t, chat.IMAGE_PROVIDER_TYPES)
        add("anthropic_api_key")
        self.assertEqual(chat.image_choices(WS), [])

    def test_choices_list_connected_enabled_capable_providers(self):
        a, b = add("openrouter"), add("openai_api_key", "OpenAI"),
        c = add("xai_api_key", "xAI")
        providers.set_enabled(c, False)
        self.assertEqual({x["label"] for x in chat.image_choices(WS)}, {"openrouter", "OpenAI"})

    def test_default_is_openrouter_and_needs_a_key(self):
        b = chat.image_backend(WS)
        self.assertEqual((b["type"], b["ok"]), ("openrouter", False))
        self.assertIn("OpenRouter, OpenAI or xAI", b["reason"])
        add("openrouter")
        self.assertTrue(chat.image_backend(WS)["ok"])

    def test_the_environment_key_still_counts_for_openrouter(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-env"}):
            self.assertTrue(chat.image_backend(WS)["ok"])

    def test_a_chosen_provider_is_used(self):
        add("openrouter")
        oid = add("openai_api_key", "OpenAI")
        config.set("workspace", WS, "image_provider", oid)
        b = chat.image_backend(WS)
        self.assertEqual((b["type"], b["label"], b["ok"]), ("openai_api_key", "OpenAI", True))

    def test_a_deleted_disabled_or_unsuitable_choice_falls_back_to_openrouter(self):
        add("openrouter")
        oid = add("openai_api_key", "OpenAI")
        config.set("workspace", WS, "image_provider", oid)
        providers.set_enabled(oid, False)
        self.assertEqual(chat.image_backend(WS)["type"], "openrouter")
        providers.set_enabled(oid, True)
        providers.delete(oid)
        self.assertEqual(chat.image_backend(WS)["type"], "openrouter")
        anth = add("anthropic_api_key")
        config.set("workspace", WS, "image_provider", anth)
        self.assertEqual(chat.image_backend(WS)["type"], "openrouter")


class Dispatch(Base):
    def test_openrouter_goes_through_the_existing_function_unchanged(self):
        add("openrouter")
        with patch.object(chat, "openrouter_image", return_value={"ok": True, "bytes": PNG, "mime": "image/png", "cost": 0.04}) as m:
            res = chat.generate_image("a cat", model="m", seed=7, references=[(b"r", "image/png")], workspace_id=WS)
        m.assert_called_once_with("a cat", model="m", seed=7, references=[(b"r", "image/png")])
        self.assertTrue(res["ok"])

    def test_no_provider_is_a_plain_reason_not_a_crash(self):
        res = chat.generate_image("a cat", model="m", workspace_id=WS)   # the OpenRouter path with no key: reported, nothing raises
        self.assertFalse(res["ok"])

    def test_a_chosen_provider_whose_key_is_gone_is_reported(self):
        oid = add("openai_api_key", "OpenAI")
        config.set("workspace", WS, "image_provider", oid)
        with patch.object(providers, "api_key_for", side_effect=ValueError("no key")):
            res = chat.generate_image("x", model="gpt-image-1", workspace_id=WS)
        self.assertFalse(res["ok"])
        self.assertIn("no key", res["reason"])


class OpenAI(Base):
    def setUp(self):
        super().setUp()
        config.set("workspace", WS, "image_provider", add("openai_api_key", "OpenAI", "sk-openai"))

    def draw(self, net, refs=None, model="gpt-image-1"):
        with patch.object(chat.urllib.request, "urlopen", net):
            return chat.generate_image("a portrait", model=model, references=refs, workspace_id=WS)

    def test_a_prompt_alone_is_a_json_generations_request(self):
        net = Net({"data": [{"b64_json": B64}], "usage": {"input_tokens": 10}})
        res = self.draw(net)
        self.assertTrue(res["ok"])
        self.assertEqual(res["bytes"], PNG)
        self.assertEqual(res["mime"], "image/png")
        self.assertIsNone(res["cost"], "OpenAI reports tokens, not dollars: the caller's estimate is used")
        self.assertEqual(net.last["url"], "https://api.openai.com/v1/images/generations")
        self.assertEqual(net.header(0, "Authorization"), "Bearer sk-openai")
        self.assertEqual(net.header(0, "Content-type"), "application/json")
        self.assertEqual(net.json(), {"model": "gpt-image-1", "prompt": "a portrait", "n": 1})

    def test_a_reference_makes_it_a_multipart_edits_request_with_the_picture(self):
        net = Net({"data": [{"b64_json": B64}]})
        res = self.draw(net, refs=[(PNG, "image/png")])
        self.assertTrue(res["ok"])
        self.assertEqual(net.last["url"], "https://api.openai.com/v1/images/edits")
        self.assertTrue(net.header(0, "Content-type").startswith("multipart/form-data; boundary="))
        body = net.last["req"].data
        self.assertIn(b'name="model"\r\n\r\ngpt-image-1', body)
        self.assertIn(b'name="prompt"\r\n\r\na portrait', body)
        self.assertIn(b'name="image[]"; filename="reference0.png"', body)
        self.assertIn(b"Content-Type: image/png", body)
        self.assertIn(PNG, body)
        boundary = net.header(0, "Content-type").split("boundary=")[1].encode()
        self.assertTrue(body.rstrip().endswith(b"--" + boundary + b"--"))

    def test_an_unknown_reference_type_is_sent_as_png(self):
        net = Net({"data": [{"b64_json": B64}]})
        self.draw(net, refs=[(PNG, "image/weird")])
        self.assertIn(b'filename="reference0.png"', net.last["req"].data)

    def test_the_providers_refusal_reads_as_a_content_refusal(self):
        net = Net(http_error(400, '{"error":{"message":"Your request was rejected by the safety system.","code":"moderation_blocked"}}'))
        res = self.draw(net)
        self.assertFalse(res["ok"])
        self.assertTrue(res["reason"].startswith("image API 400:"))
        msg, refusal = imagegen.clean_image_error(res["reason"])
        self.assertTrue(refusal)

    def test_a_timeout_is_labelled_as_one(self):
        res = self.draw(Net(urllib.error.URLError(TimeoutError("timed out"))))
        self.assertIn("timed out", res["reason"])
        self.assertFalse(imagegen.clean_image_error(res["reason"])[1])

    def test_a_reply_with_no_image_or_bad_base64_is_an_error(self):
        self.assertIn("no image", self.draw(Net({"data": []}))["reason"])
        self.assertFalse(self.draw(Net({"data": [{"b64_json": "!!not base64!!"}]}))["ok"])
        self.assertIn("rate", self.draw(Net({"error": {"message": "rate limited"}}))["reason"])

    def test_a_url_reply_is_downloaded_but_only_over_https(self):
        net = Net({"data": [{"url": "https://cdn.example/x.png"}]}, PNG)
        res = self.draw(net)
        self.assertTrue(res["ok"])
        self.assertEqual(net.requests[1]["url"], "https://cdn.example/x.png")
        self.assertFalse(self.draw(Net({"data": [{"url": "http://169.254.169.254/x"}]}))["ok"])
        self.assertFalse(self.draw(Net({"data": [{"url": "file:///etc/passwd"}]}))["ok"])

    def test_an_oversized_download_is_refused(self):
        with patch.object(chat, "IMAGE_DOWNLOAD_MAX", 10):
            res = self.draw(Net({"data": [{"url": "https://cdn.example/x.png"}]}, b"x" * 50))
        self.assertFalse(res["ok"])
        self.assertIn("oversized", res["reason"])

    def test_the_image_tools_use_it_end_to_end(self):
        net = Net({"data": [{"b64_json": B64}]})
        ref = Path(tempfile.mkdtemp()) / "neutral.png"
        ref.write_bytes(PNG)
        sent = []
        with patch.object(chat.urllib.request, "urlopen", net), patch.object(imagegen, "_REFERENCE_PATH", ref), \
                patch.object(imagegen.conversation, "add_message", lambda *a, **k: sent.append(a) or 1):
            res = imagegen.generate_selfie_impl(dict(ADMIN), "on a balcony", "hello")
        self.assertTrue(res["ok"], res)
        self.assertEqual(net.last["url"], "https://api.openai.com/v1/images/edits", "the selfie reference rides along")
        self.assertTrue(res["used_reference"])
        self.assertAlmostEqual(res["cost_usd"], float(config.get("workspace", WS, "image_cost_per_request_usd")), places=4)


class XAI(Base):
    def setUp(self):
        super().setUp()
        config.set("workspace", WS, "image_provider", add("xai_api_key", "xAI", "xai-key"))

    def draw(self, net, refs=None):
        with patch.object(chat.urllib.request, "urlopen", net):
            return chat.generate_image("a portrait", model="grok-imagine-image-2.0", references=refs, workspace_id=WS)

    def test_generations_for_a_prompt_alone(self):
        net = Net({"data": [{"b64_json": B64}]})
        self.assertTrue(self.draw(net)["ok"])
        self.assertEqual(net.last["url"], "https://api.x.ai/v1/images/generations")
        self.assertEqual(net.header(0, "Authorization"), "Bearer xai-key")
        self.assertEqual(net.json(), {"model": "grok-imagine-image-2.0", "prompt": "a portrait", "n": 1, "response_format": "b64_json"})

    def test_a_reference_goes_to_edits_as_a_data_url(self):
        net = Net({"data": [{"b64_json": B64}]})
        self.assertTrue(self.draw(net, refs=[(PNG, "image/png")])["ok"])
        self.assertEqual(net.last["url"], "https://api.x.ai/v1/images/edits")
        img = net.json()["image"]
        self.assertEqual(img["type"], "image_url")
        self.assertEqual(img["url"], "data:image/png;base64," + B64)

    def test_if_response_format_is_refused_it_asks_again_without_it(self):
        net = Net(http_error(400, '{"error":"unknown field response_format"}'), {"data": [{"url": "https://img.x.ai/a.png"}]}, PNG)
        res = self.draw(net)
        self.assertTrue(res["ok"])
        self.assertIn("response_format", net.json(0))
        self.assertNotIn("response_format", net.json(1))
        self.assertEqual(len(net.requests), 3)

    def test_any_other_error_is_not_retried(self):
        net = Net(http_error(401, '{"error":"bad key"}'))
        res = self.draw(net)
        self.assertFalse(res["ok"])
        self.assertEqual(len(net.requests), 1)
        self.assertIn("401", res["reason"])


class Configure(Base):
    def test_choosing_a_provider_resets_a_model_that_belonged_to_the_old_one(self):
        oid = add("openai_api_key", "OpenAI")
        ok, msg = imagegen.configure(WS, oid, config.get("workspace", WS, "image_model"))
        self.assertTrue(ok, msg)
        self.assertEqual(config.get("workspace", WS, "image_provider"), oid)
        self.assertEqual(config.get("workspace", WS, "image_model"), "gpt-image-1")

    def test_a_model_the_admin_typed_is_kept(self):
        oid = add("openai_api_key", "OpenAI")
        imagegen.configure(WS, oid, "gpt-image-1-mini")
        self.assertEqual(config.get("workspace", WS, "image_model"), "gpt-image-1-mini")

    def test_going_back_to_openrouter_restores_its_default(self):
        oid = add("openai_api_key", "OpenAI")
        imagegen.configure(WS, oid, "")
        ok, _ = imagegen.configure(WS, 0, "gpt-image-1")
        self.assertTrue(ok)
        self.assertEqual(config.get("workspace", WS, "image_provider"), 0)
        self.assertEqual(config.get("workspace", WS, "image_model"), chat.IMAGE_DEFAULT_MODELS["openrouter"])

    def test_a_provider_that_cannot_draw_or_does_not_exist_is_refused_and_nothing_changes(self):
        anth = add("anthropic_api_key")
        for bad in (anth, 9999, -1):
            ok, _ = imagegen.configure(WS, bad, "x")
            self.assertFalse(ok, bad)
        self.assertEqual(config.get("workspace", WS, "image_provider"), 0)
        self.assertEqual(config.get("workspace", WS, "image_model"), chat.IMAGE_DEFAULT_MODELS["openrouter"])


class _H(server.Handler):
    def __init__(self):
        self.headers = {}
        self.sent = []

    def send(self, code, body=b"", extra=None, *, ctype="text/html; charset=utf-8"):
        self.sent.append({"code": code, "body": body, "extra": extra or {}})

    def html(self):
        b = self.sent[-1]["body"]
        return b.decode() if isinstance(b, bytes) else b


class Pages(Base):
    def media_page(self):
        h = _H()
        h._settings_response = lambda sess, tab, body, **kw: h.sent.append({"code": 200, "body": body, "extra": {}})
        h.media_admin_form(dict(ADMIN))
        return h.html()

    def test_the_images_page_offers_the_connected_providers(self):
        add("openrouter")
        add("openai_api_key", "OpenAI Key")
        add("anthropic_api_key", "Claude Key")
        html = self.media_page()
        self.assertIn("name=image_provider", html)
        self.assertIn("OpenRouter (default)", html)
        self.assertIn("OpenAI Key", html)
        self.assertNotIn("Claude Key", html)
        self.assertNotIn("model (OpenRouter)", html)

    def test_saving_the_images_page_chooses_the_provider_and_resets_the_model(self):
        oid = add("openai_api_key", "OpenAI Key")
        h = _H()
        h._settings_response = lambda *a, **k: h.sent.append({"code": 200, "body": "", "extra": {}})
        h.media_admin_post(dict(ADMIN), {"image_provider": str(oid), "image_model": config.get("workspace", WS, "image_model"),
                                         "image_daily_cap_usd": "2.5"})
        self.assertEqual(config.get("workspace", WS, "image_provider"), oid)
        self.assertEqual(config.get("workspace", WS, "image_model"), "gpt-image-1")
        self.assertEqual(config.get("workspace", WS, "image_daily_cap_usd"), 2.5)

    def test_saving_a_bad_provider_reports_it(self):
        errs = []
        h = _H()
        h.media_admin_form = lambda sess, err="", info="": errs.append(err)
        h.media_admin_post(dict(ADMIN), {"image_provider": "banana"})
        self.assertTrue(errs[0])
        self.assertEqual(config.get("workspace", WS, "image_provider"), 0)

    def test_the_avatar_builder_is_ready_with_an_openai_key_once_chosen(self):
        oid = add("openai_api_key", "OpenAI Key")
        r = ab.readiness(WS)
        self.assertFalse(r["ready"], "an OpenAI key alone is not chosen yet: its model names differ, so it is never picked silently")
        self.assertEqual([c["label"] for c in r["choices"]], ["OpenAI Key"])
        h = _H()
        h.avatar_builder_page(dict(ADMIN), {})
        html = h.html()
        self.assertIn("Connect an image model first", html)
        self.assertIn("name=image_provider", html)
        self.assertIn("OpenAI Key", html)
        h = _H()
        h.avatar_build_post(dict(ADMIN), "/admin/avatars/build/model", {"image_provider": str(oid), "image_model": ""})
        self.assertNotIn("err=", h.sent[-1]["extra"]["Location"])
        r = ab.readiness(WS)
        self.assertTrue(r["ready"])
        self.assertEqual((r["type"], r["model"]), ("openai_api_key", "gpt-image-1"))

    def test_with_nothing_connected_the_builder_says_what_can_draw(self):
        h = _H()
        h.avatar_builder_page(dict(ADMIN), {})
        html = h.html()
        self.assertIn("No such provider is connected yet", html)
        self.assertNotIn("<select name=image_provider>", html)

    def test_the_builder_draws_through_the_chosen_provider(self):
        oid = add("openai_api_key", "OpenAI Key")
        imagegen.configure(WS, oid, "")
        net = Net(*[{"data": [{"b64_json": base64.b64encode(png((i * 5, 9, 9))).decode()}]} for i in range(3)])
        with patch.object(chat.urllib.request, "urlopen", net):
            ok, msg = ab.start(WS, UID, "A woman in her early thirties with short silver hair, green eyes and a charcoal cardigan.")
            self.assertTrue(ok, msg)
            t = ab._JOB["thread"]
            t.join(20)
        self.assertEqual({r["url"] for r in net.requests}, {"https://api.openai.com/v1/images/generations"})
        self.assertEqual([c["status"] for c in ab.status()["candidates"]], ["done"] * 3)
        ab.discard()


if __name__ == "__main__":
    unittest.main()
