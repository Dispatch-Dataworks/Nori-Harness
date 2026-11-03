"""Inline script for a model dropdown that follows a provider dropdown: fills a <select> from a JSON list for the chosen provider, with a type-a-name
fallback. Used for drawing pictures (Settings -> Images and the avatar builder setup card) and for reading them (Settings -> Images)."""

_TEMPLATE = '<script>(function(){var p=document.querySelector("select[name=__PROVIDER__]"),w=document.getElementById("__FIELD__");\nif(!p||!w)return;var sel=w.querySelector("select"),txt=w.querySelector("input[type=text]"),note=w.querySelector(".img-note");\nvar saved=txt.value;\nfunction fill(items,def){var cur=sel.value&&sel.value!="__other"?sel.value:saved;sel.innerHTML="";\nvar seen=false;items.forEach(function(m){var o=document.createElement("option");o.value=m.id;o.textContent=m.label+(m.id==def?" (default)":"");\nif(m.id==cur){o.selected=true;seen=true}sel.appendChild(o)});\nvar o=document.createElement("option");o.value="__other";o.textContent="other (type a name)...";sel.appendChild(o);\nif(!seen){if(cur&&cur!=def){sel.value="__other";txt.value=cur}else if(def&&items.some(function(m){return m.id==def})){sel.value=def}else{sel.value="__other"}}\nsync()}\nfunction sync(){if(sel.value=="__other"){txt.style.display="";txt.name="__MODEL__";sel.name=""}else{txt.style.display="none";txt.name="";sel.name="__MODEL__"}}\nfunction load(){note.textContent="loading models...";sel.disabled=true;\nfetch("__URL__?provider="+encodeURIComponent(p.value),{credentials:"same-origin"}).then(function(r){return r.json()}).then(function(d){\nsel.disabled=false;fill(d.items||[],d.default||"");note.textContent=d.ok?(d.items.length+" __NOUN__ available"):("couldn\'t load the list ("+(d.error||"error")+"): type a name instead")})\n.catch(function(){sel.disabled=false;sel.value="__other";sync();note.textContent="couldn\'t load the list: type a name instead"})}\nsel.addEventListener("change",sync);p.addEventListener("change",function(){saved="";txt.value="";load()});load()})();</script>'


def script(*, provider: str, field: str, url: str, model: str, noun: str) -> str:
    """provider: the provider <select>'s name; field: the id of the element holding the model <select>, the text <input> and a .img-note; url: the
    JSON list ({ok, items, error, default}); model: the form field name the chosen model is posted as; noun: what the count under the list counts."""
    return (_TEMPLATE.replace("__PROVIDER__", provider).replace("__FIELD__", field).replace("__URL__", url)
            .replace("__MODEL__", model).replace("__NOUN__", noun))


JS = script(provider="image_provider", field="image-model-field", url="/admin/image-models", model="image_model", noun="image models")
VISION_JS = script(provider="vision_provider", field="vision-model-field", url="/admin/vision-models", model="vision_model",
                   noun="models that read pictures")
