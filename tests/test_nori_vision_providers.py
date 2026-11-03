# Copyright (C) 2026 Dispatch Dataworks LLC. Lead Researcher: Benjamin Townsend.
#
# This file is part of Nori, licensed under the GNU Affero General Public
# License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE
# file at the root of this repository, or
# <https://www.gnu.org/licenses/agpl-3.0.html>.

"""Reading and drawing pictures through any connection (2026-11-03). Found live: every picture read went to one OpenRouter model, so an empty
OpenRouter balance stopped all of them while a ChatGPT sign-in whose model reads pictures sat unused; and drawing was limited to API keys.

Pinned: vision_provider/vision_model choose who reads pictures, with OpenRouter as the default and the fallback for a choice that is gone; a picture
reaches every adapter in that adapter's own format (Responses input_image, Anthropic image blocks, Copilot's vision header); configure_vision checks
the connection and resets the model when the connection changes; the model lists for the dropdown come from each provider's own capability data, with
the models already in use first; the ChatGPT sign-in draws with the image_generation tool and the Grok sign-in through api.x.ai with its token; and
Settings -> Images shows and saves both. Real SQLite and providers table; only the network is faked."""
import base64
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["NORI_DATA_DIR"] = tempfile.mkdtemp(prefix="nori_test_vision_prov_")
os.environ["NORI_PROMPTS_DIR"] = tempfile.mkdtemp(prefix="nori_test_vision_prov_prompts_")
os.environ["NORI_NO_LOGFILE"] = "1"
os.environ.pop("OPENROUTER_API_KEY", None)
for _f in ("persona.default.md", "persona.template.md"):
    shutil.copy(ROOT / "prompts" / _f, os.environ["NORI_PROMPTS_DIR"])

import accounts  # noqa: E402
import avatar_builder as ab  # noqa: E402
import chat  # noqa: E402
import config  # noqa: E402
import crypto  # noqa: E402
import imagegen  # noqa: E402
import models  # noqa: E402
import providers  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

store.init()
_admin = accounts.bootstrap_admin("Ben", "testpass123")
UID, WS = _admin["id"], _admin["workspace_id"]
ADMIN = {"user_id": UID, "workspace_id": WS, "role": "admin", "csrf": "csrf-tok"}

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def disconnect():
    store.write(lambda c: c.execute("DELETE FROM models"))
    store.write(lambda c: c.execute("DELETE FROM providers"))


def add(type_, label=None):
    """A connected provider of any type (OAuth ones inserted directly; their tokens are faked with access_token_for)."""
    label = label or type_
    store.write(lambda c: c.execute(
        "INSERT INTO providers(workspace_id, type, label, status, api_key_enc, enabled, created_ts, created_by) VALUES (?,?,?,?,?,1,?,?)",
        (WS, type_, label, "connected", crypto.encrypt("sk-test"), time.time(), UID)))
    return [p for p in providers.list_all(WS) if p["label"] == label][-1]["id"]


def add_model(provider_id, name):
    ok, msg = models.create(name, provider_id, name, None)
    assert ok, msg


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
    """Fake urlopen: answers from a queue (dict -> JSON, bytes raw, exception raised) and records each request."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return Resp(r if isinstance(r, bytes) else json.dumps(r).encode())

    def body(self, i=-1):
        return json.loads(self.requests[i].data.decode())


def sse(*events) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


class Base(unittest.TestCase):
    def setUp(self):
        disconnect()
        for k, v in (("vision_provider", 0), ("vision_model", ""), ("image_provider", 0),
                     ("image_model", chat.IMAGE_DEFAULT_MODELS["openrouter"])):
            config.set("workspace", WS, k, v)
        self._tok = patch.object(providers, "access_token_for", return_value="oauth-token")
        self._tok.start()
        self.addCleanup(self._tok.stop)


class Formats(unittest.TestCase):
    PARTS = [{"type": "text", "text": "what is this?"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]

    def test_responses_input_carries_the_picture(self):
        _, items = chat._messages_to_responses_input([{"role": "user", "content": self.PARTS}])
        self.assertEqual(items[0]["content"], [{"type": "input_text", "text": "what is this?"},
                                               {"type": "input_image", "image_url": "data:image/png;base64,QUJD"}])

    def test_anthropic_gets_an_image_block(self):
        _, msgs = chat._messages_to_anthropic([{"role": "user", "content": self.PARTS}])
        self.assertEqual(msgs[0]["content"][1], {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}})
        _, msgs = chat._messages_to_anthropic([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}]}])
        self.assertEqual(msgs[0]["content"][0]["source"], {"type": "url", "url": "https://x/y.png"})

    def test_plain_text_is_unchanged(self):
        _, items = chat._messages_to_responses_input([{"role": "user", "content": "hi"}])
        self.assertEqual(items[0]["content"], "hi")
        _, msgs = chat._messages_to_anthropic([{"role": "user", "content": "hi"}])
        self.assertEqual(msgs[0]["content"], "hi")

    def test_copilot_says_when_a_picture_is_coming(self):
        self.assertEqual(chat._copilot_vision_header([{"role": "user", "content": self.PARTS}]), {"copilot-vision-request": "true"})
        self.assertEqual(chat._copilot_vision_header([{"role": "user", "content": "hi"}]), {})


class Backend(Base):
    def test_default_is_openrouter_with_the_vision_model(self):
        b = chat.vision_backend(WS)
        self.assertEqual((b["type"], b["model"], b["ok"]), ("openrouter", chat.VISION_MODEL, False))
        add("openrouter")
        self.assertTrue(chat.vision_backend(WS)["ok"])

    def test_every_connected_type_can_be_chosen(self):
        for t in ("openai_oauth", "github_copilot", "anthropic_oauth", "xai_oauth", "openai_api_key", "anthropic_api_key", "xai_api_key"):
            add(t)
        self.assertEqual(len(chat.vision_choices(WS)), 7)

    def test_a_chosen_connection_defaults_to_the_model_already_used_with_it(self):
        pid = add("openai_oauth", "OpenAI")
        add_model(pid, "gpt-5.6-luna")
        config.set("workspace", WS, "vision_provider", pid)
        b = chat.vision_backend(WS)
        self.assertEqual((b["type"], b["model"], b["ok"]), ("openai_oauth", "gpt-5.6-luna", True))
        self.assertEqual(chat.vision_label(WS), "OpenAI (gpt-5.6-luna)")

    def test_a_chosen_connection_with_no_model_says_so(self):
        config.set("workspace", WS, "vision_provider", add("openai_oauth", "OpenAI"))
        b = chat.vision_backend(WS)
        self.assertFalse(b["ok"])
        self.assertIn("Settings -> Images", b["reason"])

    def test_a_deleted_or_disabled_choice_falls_back_to_openrouter_and_its_model(self):
        add("openrouter")
        pid = add("anthropic_api_key", "Claude")
        config.set("workspace", WS, "vision_provider", pid)
        config.set("workspace", WS, "vision_model", "claude-opus-5-5")
        providers.set_enabled(pid, False)
        b = chat.vision_backend(WS)
        self.assertEqual((b["type"], b["model"]), ("openrouter", chat.VISION_MODEL), "a Claude model name means nothing at OpenRouter")


class Configure(Base):
    def test_choosing_a_connection_resets_a_model_from_the_old_one(self):
        pid = add("github_copilot", "GitHub")
        add_model(pid, "gpt-5.6-luna")
        ok, msg = chat.configure_vision(WS, pid, chat.VISION_MODEL)
        self.assertTrue(ok, msg)
        self.assertEqual(config.get("workspace", WS, "vision_model"), "gpt-5.6-luna")
        ok, _ = chat.configure_vision(WS, pid, "claude-sonnet-4.5")
        self.assertEqual(config.get("workspace", WS, "vision_model"), "claude-sonnet-4.5")
        ok, _ = chat.configure_vision(WS, 0, "")
        self.assertEqual((config.get("workspace", WS, "vision_provider"), config.get("workspace", WS, "vision_model")), (0, chat.VISION_MODEL))

    def test_refusals(self):
        self.assertFalse(chat.configure_vision(WS, 9999, "x")[0])
        pid = add("openai_oauth", "OpenAI")
        ok, msg = chat.configure_vision(WS, pid, "")
        self.assertFalse(ok)
        self.assertIn("type the name", msg)
        self.assertEqual(config.get("workspace", WS, "vision_provider"), 0)


class Reading(Base):
    def test_openrouter_uses_the_connected_key(self):
        add("openrouter")
        with patch.object(chat, "call", return_value={"content": " a cat "}) as m:
            self.assertEqual(chat.vision("describe", [(PNG, "image/png")], workspace_id=WS), "a cat")
        kw = m.call_args.kwargs
        self.assertEqual((kw["model"], kw["api_key"], kw["tools"]), (chat.VISION_MODEL, "sk-test", None))

    def test_the_chatgpt_sign_in_reads_the_picture(self):
        pid = add("openai_oauth", "OpenAI")
        add_model(pid, "gpt-5.6-luna")
        config.set("workspace", WS, "vision_provider", pid)
        net = Net(sse({"type": "response.output_text.delta", "delta": "A grey square."},
                      {"type": "response.completed", "response": {"status": "completed", "usage": {}}}))
        with patch("urllib.request.urlopen", net):
            self.assertEqual(chat.vision("describe", [(PNG, "image/png")], workspace_id=WS), "A grey square.")
        body = net.body()
        self.assertEqual(body["model"], "gpt-5.6-luna")
        self.assertNotIn("tools", body)
        parts = body["input"][0]["content"]
        self.assertEqual(parts[0], {"type": "input_text", "text": "describe"})
        self.assertTrue(parts[1]["image_url"].startswith("data:image/png;base64,"))

    def test_claude_reads_the_picture(self):
        pid = add("anthropic_api_key", "Claude")
        config.set("workspace", WS, "vision_provider", pid)
        config.set("workspace", WS, "vision_model", "claude-opus-5-5")
        net = Net({"content": [{"type": "text", "text": "A grey square."}], "usage": {}})
        with patch("urllib.request.urlopen", net):
            self.assertEqual(chat.vision("describe", [(PNG, "image/png")], workspace_id=WS), "A grey square.")
        self.assertEqual(net.body()["messages"][0]["content"][1]["type"], "image")

    def test_copilot_sends_the_vision_header(self):
        pid = add("github_copilot", "GitHub")
        config.set("workspace", WS, "vision_provider", pid)
        config.set("workspace", WS, "vision_model", "gpt-5.6-luna")
        net = Net({"choices": [{"message": {"content": "A grey square."}, "finish_reason": "stop"}], "usage": {}})
        with patch("urllib.request.urlopen", net):
            self.assertEqual(chat.vision("describe", [(PNG, "image/png")], workspace_id=WS), "A grey square.")
        self.assertEqual(net.requests[0].get_header("Copilot-vision-request"), "true")

    def test_no_connection_is_a_model_error(self):
        with self.assertRaises(chat.ModelError):
            chat.vision("describe", [(PNG, "image/png")], workspace_id=WS)

    def test_the_avatar_builder_uses_the_chosen_connection(self):
        pid = add("openai_oauth", "OpenAI")
        add_model(pid, "gpt-5.6-luna")
        config.set("workspace", WS, "vision_provider", pid)
        route = ab.vision_route(WS)
        self.assertEqual((route["kind"], route["model"], route["_entry"]["provider"]["id"]), ("chosen", "gpt-5.6-luna", pid))


class ModelLists(Base):
    def test_openrouter_lists_models_that_take_pictures(self):
        net = Net({"data": [{"id": "a/sees", "name": "Sees", "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]}},
                            {"id": "a/blind", "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
                            {"id": "a/draws", "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["image"]}}]})
        with patch("urllib.request.urlopen", net):
            ok, items = providers.list_vision_models(None)
        self.assertEqual((ok, [m["id"] for m in items]), (True, ["a/sees"]))

    def test_copilot_lists_by_its_vision_capability_with_the_models_in_use_first(self):
        pid = add("github_copilot", "GitHub")
        add_model(pid, "gpt-5.6-luna")
        net = Net({"data": [{"id": "claude-sonnet-4.5", "capabilities": {"supports": {"vision": True}}},
                            {"id": "o3-mini", "capabilities": {"supports": {"vision": False}}}]})
        with patch("urllib.request.urlopen", net):
            ok, items = providers.list_vision_models(providers.get(pid))
        self.assertEqual([m["id"] for m in items], ["gpt-5.6-luna", "claude-sonnet-4.5"])

    def test_openai_drops_models_that_cannot_see(self):
        pid = add("openai_api_key", "OpenAI API")
        net = Net({"data": [{"id": i} for i in ("gpt-5.5", "gpt-4o-mini-tts", "text-embedding-3-small", "gpt-image-1", "o3", "whisper-1")]})
        with patch("urllib.request.urlopen", net):
            ok, items = providers.list_vision_models(providers.get(pid))
        self.assertEqual([m["id"] for m in items], ["gpt-5.5", "o3"])

    def test_the_chatgpt_sign_in_offers_the_models_in_use(self):
        pid = add("openai_oauth", "OpenAI")
        self.assertFalse(providers.list_vision_models(providers.get(pid))[0])
        add_model(pid, "gpt-5.6-luna")
        ok, items = providers.list_vision_models(providers.get(pid))
        self.assertEqual((ok, [m["id"] for m in items]), (True, ["gpt-5.6-luna"]))


class Drawing(Base):
    def test_the_sign_ins_can_draw_and_claude_and_copilot_cannot(self):
        for t in ("openai_oauth", "xai_oauth", "anthropic_oauth", "github_copilot"):
            add(t)
        self.assertEqual({c["type"] for c in chat.image_choices(WS)}, {"openai_oauth", "xai_oauth"})

    def test_the_grok_sign_in_draws_through_api_x_ai_with_its_token(self):
        config.set("workspace", WS, "image_provider", add("xai_oauth", "Grok"))
        net = Net({"data": [{"b64_json": base64.b64encode(PNG).decode()}]})
        with patch("urllib.request.urlopen", net):
            res = chat.generate_image("a cat", model="grok-imagine-image-2.0", workspace_id=WS)
        self.assertTrue(res["ok"], res)
        self.assertEqual(net.requests[0].get_full_url(), "https://api.x.ai/v1/images/generations")
        self.assertEqual(net.requests[0].get_header("Authorization"), "Bearer oauth-token")

    def test_the_chatgpt_sign_in_draws_with_the_image_tool(self):
        pid = add("openai_oauth", "OpenAI")
        add_model(pid, "gpt-5.6-luna")
        ok, msg = imagegen.configure(WS, pid, "")
        self.assertTrue(ok, msg)
        self.assertEqual(config.get("workspace", WS, "image_model"), "gpt-5.6-luna")
        net = Net(sse({"type": "response.output_item.done", "item": {"type": "image_generation_call", "result": base64.b64encode(PNG).decode()}},
                      {"type": "response.completed", "response": {"status": "completed", "usage": {}}}))
        with patch("urllib.request.urlopen", net):
            res = chat.generate_image("a cat", model="gpt-5.6-luna", references=[(PNG, "image/png")], workspace_id=WS)
        self.assertEqual((res["ok"], res["bytes"], res["cost"]), (True, PNG, None))
        body = net.body()
        self.assertEqual((body["model"], body["tools"], body["store"]), ("gpt-5.6-luna", [{"type": "image_generation"}], False))
        self.assertEqual([p["type"] for p in body["input"][0]["content"]], ["input_text", "input_image"])

    def test_the_chatgpt_sign_in_without_a_picture_reports_what_it_said(self):
        config.set("workspace", WS, "image_provider", add("openai_oauth", "OpenAI"))
        net = Net(sse({"type": "response.output_text.delta", "delta": "I can't draw that."},
                      {"type": "response.completed", "response": {"status": "completed"}}))
        with patch("urllib.request.urlopen", net):
            res = chat.generate_image("a cat", model="gpt-5.6-luna", workspace_id=WS)
        self.assertFalse(res["ok"])
        self.assertIn("I can't draw that.", res["reason"])

    def test_choosing_the_chatgpt_sign_in_needs_a_model(self):
        ok, msg = imagegen.configure(WS, add("openai_oauth", "OpenAI"), "")
        self.assertFalse(ok)
        self.assertIn("type the name", msg)


class _Handler(server.Handler):
    def __init__(self):
        self.headers = {}
        self.sent = []

    def send(self, code, body=b"", ctype="text/html; charset=utf-8", extra=None):
        self.sent.append({"code": code, "body": body if isinstance(body, bytes) else body.encode(), "extra": extra or {}})

    def send_json(self, obj, code=200):
        self.sent.append({"code": code, "json": obj})


class SettingsPage(Base):
    def test_the_images_page_has_both_dropdowns(self):
        add("openai_oauth", "OpenAI")
        h = _Handler()
        with patch.object(server.Handler, "_settings_response", lambda self, sess, tab, body: self.sent.append({"html": body})):
            h.media_admin_form(dict(ADMIN))
        html = h.sent[-1]["html"]
        for needle in ("name=image_provider", "name=vision_provider", "id=vision-model-field", "/admin/vision-models", "Reading pictures now"):
            self.assertIn(needle, html)
        self.assertIn(">OpenAI</option>", html.split("name=vision_provider")[1].split("</select>")[0])

    def test_saving_the_picture_reader(self):
        pid = add("openai_oauth", "OpenAI")
        h = _Handler()
        with patch.object(server.Handler, "media_admin_form", lambda self, sess, err="", info="": self.sent.append({"err": err})):
            h.media_admin_post(dict(ADMIN), {"vision_provider": str(pid), "vision_model": "gpt-5.6-luna"})
        self.assertEqual(h.sent[-1]["err"], "")
        self.assertEqual((config.get("workspace", WS, "vision_provider"), config.get("workspace", WS, "vision_model")), (pid, "gpt-5.6-luna"))

    def test_the_model_list_endpoint(self):
        pid = add("openai_oauth", "OpenAI")
        add_model(pid, "gpt-5.6-luna")
        h = _Handler()
        h.vision_models_get(dict(ADMIN), {"provider": [str(pid)]})
        self.assertEqual(h.sent[-1]["json"]["items"][0]["id"], "gpt-5.6-luna")
        self.assertEqual(h.sent[-1]["json"]["default"], "gpt-5.6-luna")
        h.vision_models_get(dict(ADMIN), {"provider": ["9999"]})
        self.assertFalse(h.sent[-1]["json"]["ok"])
        h.vision_models_get(dict(ADMIN, role="member"), {"provider": ["0"]})
        self.assertNotIn("json", h.sent[-1])


if __name__ == "__main__":
    unittest.main()
