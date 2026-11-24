"""Explicit local WebView operations; credentials and file paths remain in the host."""
import sqlite3
from threading import RLock

from .config import ConfigStore
from .corpus_files import read_supplied
from .desktop_client import LocalClient
from .storage import Store
from .import_library import ImportLibrary, require
from .tk_notice import CONTRACT
from .models import PersonaSpec, RuntimeErrorBase


class WorkbenchData:
    def __init__(self, paths, window):
        self._config=ConfigStore(paths)
        self._client=LocalClient(self._config,bind_instance=True)
        from .workbench_submissions import WorkbenchSubmissions
        self._submissions = WorkbenchSubmissions(self._config, self._client)
        from .workbench_presentation import WorkbenchPresentation
        self._presentation = WorkbenchPresentation(self._config,self._client)
        self._library=ImportLibrary(paths)
        self._window=window
        self._selection_lock=RLock()
        self._activation = None
        self._deepseek = None
        self._import_selection = None
        self._repair_status=None
        self._client.write_check=self._require_runtime_write

    def _require_runtime_write(self):
        from .local_repair import require_open,RepairInProgress,ACTIVE
        if self._repair_status:
            record=self._repair_status()
            if record and record['phase'] in ACTIVE:raise RepairInProgress()
        require_open(self._config.paths,self._client.expected_instance(self._config.load()))

    def _require_open(self):
        self._require_runtime_write()
        if self._activation is not None:
            self._activation.require()

    def pick_transcript(self, choose_scope=False):
        self._require_open()
        from webview import FileDialog
        names=self._window().create_file_dialog(FileDialog.OPEN,allow_multiple=False,file_types=("Markdown (*.md)",))
        if not names:
            return None
        require(names[0].lower().endswith(".md"),"请选择 Markdown 文件。")
        raw=read_supplied(names[0]);self._require_open()
        if choose_scope:
            from .import_markdown import parse
            return self._stage_import({"raw": raw, "parsed": parse(raw)})
        return self._library.add(raw)

    def _stage_import(self, source):
        from .models import PRIVATE_CHAT, identifier
        with self._selection_lock:
            token = identifier()
            self._import_selection = (token, source)
            if "deepseek" in source:
                return {"token": token, "total": len(source["deepseek"]["rows"]), "chat_mode": PRIVATE_CHAT,
                        "title": source["deepseek"]["conversation"]["title"]}
            return {"token": token, "total": len(source["parsed"]["rows"]),
                    "chat_mode": source["parsed"].get("chat_mode", PRIVATE_CHAT)}

    def commit_import_scope(self, token, count, mode, confirmed=False):
        from .deepseek_intake import DeepSeekInbox, archive_identity
        from .import_scope import selected
        self._require_open()
        with self._selection_lock:
            require(self._import_selection is not None and token == self._import_selection[0],
                    "导入来源已变化，请重新选择。")
            source = self._import_selection[1]
            if "deepseek" in source:
                path, snapshot, task, preference = source["origin"]
                require(snapshot == archive_identity(path), "导出文件已变化，请重新选择。")
            raw, deepseek = selected(source, count, mode, confirmed)
            self._require_open()
            document = self._library.add(raw, deepseek_source=deepseek)
            if deepseek:
                inbox = DeepSeekInbox(self._config.paths)
                if task:
                    inbox.finish(task["id"], "imported")
                elif preference:
                    inbox.forget(preference["job"])
            self._import_selection = None
            return document

    def clear_deepseek_key(self):
        return self._client.manage("desktop.activation.clear")

    def verify_deepseek_key(self, key="", confirmation=None):
        return self._client.call("POST", "/v1/manage", {"action": "desktop.activation.verify",
            "args": {"key": key, "confirm": confirmation}}, timeout=25)

    def _set_deepseek_file(self, path, task=None):
        from pathlib import Path
        from .deepseek_export import inspect
        from .deepseek_intake import archive_identity
        self._require_open()
        path = Path(path)
        snapshot = archive_identity(path)
        if task is not None:
            require(snapshot == task["identity"], "浏览器下载文件已变化，请重新选择。")
        data = inspect(path)
        require(snapshot == archive_identity(path), "导出文件在读取时发生变化，请重新选择。")
        from .deepseek_intake import DeepSeekInbox
        preference = None if task else DeepSeekInbox(self._config.paths).preferred()
        target = (task or preference or {}).get("target")
        with self._selection_lock:
            self._deepseek = (path, snapshot, task, preference)
        return data | {"target": target}

    def pick_deepseek_export(self):
        self._require_open()
        from webview import FileDialog
        names = self._window().create_file_dialog(FileDialog.OPEN, allow_multiple=False, file_types=("DeepSeek ZIP (*.zip)",))
        if names:
            return self._set_deepseek_file(names[0])
        return None

    def deepseek_pending(self):
        from .deepseek_intake import DeepSeekInbox
        task = DeepSeekInbox(self._config.paths).pending()
        return {"id": task["id"]} if task else None

    def open_deepseek_pending(self, ident):
        from .deepseek_intake import DeepSeekInbox
        self._require_open()
        task = DeepSeekInbox(self._config.paths).pending()
        require(task and task["id"] == ident, "浏览器导入任务已变化。")
        return self._set_deepseek_file(task["path"], task)

    def discard_deepseek_pending(self, ident):
        from .deepseek_intake import DeepSeekInbox
        self._require_open()
        inbox = DeepSeekInbox(self._config.paths)
        task = inbox.pending()
        require(task and task["id"] == ident, "浏览器导入任务已变化。")
        inbox.finish(ident, "cancelled")

    def import_deepseek_session(self, ident, branch=None):
        from .deepseek_export import inspect
        from .deepseek_intake import archive_identity
        self._require_open()
        with self._selection_lock:
            require(self._deepseek is not None, "请先选择 DeepSeek 导出文件。")
            path, snapshot, task, preference = self._deepseek
            require(snapshot == archive_identity(path), "导出文件已变化，请重新选择。")
            picked = inspect(path, ident, branch, prepare=True)
            require(snapshot == archive_identity(path), "导出文件在读取时发生变化，请重新选择。")
            if "rows" not in picked:
                return picked
            return {"scope": self._stage_import({"deepseek": picked, "origin": self._deepseek})}

    def revise_deepseek_markdown(self, ident, edits):
        from .deepseek_export import revise_markdown
        from .import_markdown import DEEPSEEK_FORMAT, parse
        self._require_open()
        original = self._library.row(ident)
        source = self._library.deepseek_source(ident)
        require(source and original["format"] == DEEPSEEK_FORMAT and original["status"] != "revoked",
                "DeepSeek 来源已不可用。")
        raw, summary = source
        markdown = revise_markdown(original["raw"], summary["id"], summary["branch"], edits)
        if markdown == original["raw"]:
            return self._library.status(ident)
        rows = parse(markdown)
        dates = [row["date"] for row in rows["rows"] if row["date"]]
        return self._library.add(markdown, deepseek_source=(raw, summary | {
            "counts": rows["counts"], "from": dates[0] if dates else None,
            "to": dates[-1] if dates else None, "edited_from": ident,
        }))

    def save_deepseek_markdown(self, ident):
        from pathlib import Path
        from webview import FileDialog
        from .import_markdown import DEEPSEEK_FORMAT
        self._require_open()
        row = self._library.row(ident)
        require(row["format"] == DEEPSEEK_FORMAT and row["status"] != "revoked", "DeepSeek 来源已不可用。")
        chosen = self._window().create_file_dialog(FileDialog.SAVE, save_filename="DeepSeek.md",
                                                    file_types=("Markdown (*.md)",))
        if chosen:
            self._require_open()
            target = Path(chosen if isinstance(chosen, str) else chosen[0])
            require(target.suffix.lower() == ".md", "请选择 Markdown 文件。")
            target.write_bytes(row["raw"])
        return bool(chosen)

    def import_prompt_file(self):
        self._require_open()
        from webview import FileDialog
        from .import_prompts import import_prompt_file
        names = self._window().create_file_dialog(
            FileDialog.OPEN, allow_multiple=False, file_types=("PCAR 提示词 (*.json)",))
        self._require_open()
        return import_prompt_file(self._library, names[0]) if names else None

    def import_action(self, name, args):
        require(name in ("prompt_status","list","replay","analyze","status","cancel","evidence","prepare","edit","create","style","feedback",
                         "card_preview","card_status","card_compose","card_edit","card_review","card_create","card_scene_status","card_scene_enable",
                         "update","revoke","history","history_preview","history_commit","history_status","history_update","plan","plan_evidence","plan_archive","review","review_all"),"未知导入操作。")
        pid = args.get("persona_id")
        if pid and name in ("history_preview","history_update","update","card_scene_enable"):
            self._use_persona(pid)
        return self._client.call("POST","/v1/manage",{"action":"persona.import."+name,"args":args},timeout=180)

    def live_health(self):
        from .local_repair import read,public,ACTIVE
        from .maintenance import storage_fault_pending
        repair=self._repair_status() if self._repair_status else public(read(self._config.paths))
        if repair and repair['phase'] in ACTIVE:return {'state':'repairing','repair':repair}
        if storage_fault_pending(self._config.paths):
            return {'state':'repairing','repair':{'phase':'read_only','action':'check_storage','reason':'storage_unavailable'}}
        cfg=self._config.load()
        from .desktop_client import LocalAPIError, TransportUnknown
        try:
            status=self._client.call("GET","/v1/health?view=desktop",timeout=2)
        except TransportUnknown:
            return {"state":"disconnected",**({"repair":repair} if repair else {})}
        except LocalAPIError as error:
            return {"state":"wrong_instance" if error.status in (401,403,409) else "disconnected",**({"repair":repair} if repair else {})}
        if status.get("instance_id")!=cfg["instance_id"] or status.get("service")!="persona-runtime":
            return {"state":"wrong_instance"}
        return {"state":"online","generation":status.get("service_generation"),"configured":bool(cfg["model"]["encrypted_key"] and cfg["model"]["base_url"]),
                "model":cfg["model"]["name"],"activation":self._activation.status(cfg) if self._activation else None,"configVersion":str(self._config.file.stat().st_mtime_ns),
                "draining":bool(status.get("draining")),**({"quotaWaits":status["quota_waits"]} if status.get("quota_waits") else {}),
                **({"maintenance":status["maintenance"]} if status.get("maintenance") else {}),
                **({"repair":repair} if repair else {})}

    def live_quota_settings(self,settings=None):
        if settings is None:return self._client.manage('quota.settings.current')
        return self._client.manage('quota.configure',**settings)

    def export_support_logs(self, service_state=None, key_state=None):
        from .diagnostic_export import export_logs

        path = export_logs(self._config.paths, service_state, key_state)
        return {"name": path.name, "path": str(path)}

    def live_backups(self):
        from .automatic_backup import Backups
        self._require_open()
        return Backups(self._config.paths).status()

    def restore_backup_copy(self,name=None):
        from pathlib import Path
        from webview import FileDialog
        from .automatic_backup import Backups
        from .models import identifier
        self._require_open()
        selection=self._window().create_file_dialog(FileDialog.FOLDER,allow_multiple=False)
        if not selection:return None
        self._require_open()
        target=Path(selection[0])/('PCAR-恢复核对-'+identifier()[:8])
        return Backups(self._config.paths).restore(target,name)

    def live_quota_decide(self,ids,allow):
        return self._client.manage('quota.workflow.decide',ids=ids,allow=allow)

    def _use_persona(self,pid):
        self._client.manage("persona.use",persona_id=pid)

    def live_network_wake(self):
        return self._client.manage('desktop.network.wake')

    def live_delete_persona(self, pid, receipt=None):
        self._require_open()
        if receipt is None:
            return self._client.manage('persona.inspect',persona_id=pid)
        require(isinstance(receipt,dict) and set(receipt)=={'expected_revision','expected_selected','operation_id'},
                "删除人物确认无效。")
        with self._selection_lock:
            return self._client.call('POST','/v1/manage',{'action':'persona.purge',
                'args':{'persona_id':pid,**receipt}},timeout=120)

    def live_people(self):
        config=self._config.public()["model"]
        return {"people":self._client.manage("persona.list"),"connected":bool(config.get("has_key") and config.get("base_url")),
                "model":config["name"]}

    def live_persona_image(self, pid, kind):
        from .workbench_appearance import image_path, image_url
        paths = self._config.paths
        return image_url(image_path(paths, pid, kind), kind)

    def live_choose_persona_image(self, pid, kind):
        from webview import FileDialog
        from .workbench_appearance import image_path, image_url, prepare_image, store_image

        self._require_open()
        paths = self._config.paths
        image_path(paths, pid, kind)
        names = self._window().create_file_dialog(
            FileDialog.OPEN, allow_multiple=False, file_types=("图片 (*.png;*.jpg;*.jpeg;*.webp)",))
        if not names:
            return {"changed": False}
        try:
            prepared = prepare_image(names[0], kind)
        except RuntimeErrorBase as error:
            return {"changed": False, "error": str(error)}
        self._require_open()
        with self._selection_lock:
            path = image_path(paths, pid, kind)
            store_image(paths, path, prepared)
            return {"changed": True, "url": image_url(path, kind)}

    def live_reset_persona_image(self, pid, kind):
        from .workbench_appearance import image_path
        self._require_open()
        with self._selection_lock:
            image_path(self._config.paths, pid, kind).unlink(missing_ok=True)
        return {"changed": True, "url": None}

    def live_select(self,pid,fresh=False,view_session_id=None,title=None):
        self._require_open()
        with self._selection_lock:
            if fresh:
                require(view_session_id is None,"浏览已保存会话时无需新建。")
                args={"title":title} if title is not None else {}
                self._client.manage("session.select",channel="desktop",fresh=True,persona_id=pid,**args)
            return self._client.manage("desktop.browser.show",persona_id=pid)

    def live_rename_session(self,pid,sid,title):
        self._require_open()
        with self._selection_lock:
            return self._client.manage("desktop.browser.rename",pid=pid,sid=sid,value=title)

    def live_branch(self, pid, sid, anchor, operation_id):
        self._require_open()
        with self._selection_lock:
            self._client.manage('persona.use', persona_id=pid)
            return self._client.manage('session.branch', persona_id=pid, session_id=sid,
                                       anchor=anchor, operation_id=operation_id)

    def live_activate(self, pid, sid, operation_id):
        self._require_open()
        with self._selection_lock:
            self._client.manage('persona.use', persona_id=pid)
            return self._client.manage('session.activate_desktop', persona_id=pid, session_id=sid,
                                       operation_id=operation_id)

    def live_message_position(self, pid, sid, target):
        return self._client.manage('desktop.message.position', pid=pid, sid=sid, target=target)

    def live_snapshot(self,sid,version=None):
        return self._client.manage("chat.snapshot",session_id=sid,version=version,include_imports=False)

    def live_privacy(self,pid,sid,decision=None):
        self._require_open()
        if not sid:
            sid=self._client.manage("session.select",persona_id=pid,channel="desktop")["session_id"]
            self._submissions.first_session(pid,sid)
        if decision is None:
            result=self._client.manage("desktop.privacy.status",persona_id=pid,session_id=sid)
        else:
            result=self._client.manage("desktop.privacy.update",persona_id=pid,session_id=sid,**decision)
        return result|{"session_id":sid}

    def live_connection_change(self,role,base_url,name,key,confirm=None):
        self._require_open()
        return self._client.manage("desktop.privacy.connection",role=role,base_url=base_url,
                                   name=name,key=key,confirm=confirm)

    def live_main_reply(self, change=None):
        self._require_open()
        if change is None:
            return self._client.manage('desktop.main_reply.status')
        return self._client.manage('desktop.main_reply.configure',**change)

    def live_main_reply_consent(self,pid,sid,enabled,revision,recipient_hash):
        self._require_open()
        return self._client.manage('desktop.main_reply.consent',persona_id=pid,session_id=sid,
                                   enabled=enabled,revision=revision,recipient_hash=recipient_hash)

    def live_page(self,pid,sid,version=None,before=None,after=None,anchor=None):
        return self._client.manage("desktop.timeline.snapshot",pid=pid,sid=sid,version=version,before=before,after=after,anchor=anchor)

    def live_set_buffer(self,pid,mode,quiet_ms,manual,expected_version,operation_id):
        self._require_open()
        selected=self._client.manage("persona.show")["id"]
        return self._client.manage("interaction.update",persona_id=pid,channel="desktop",mode=mode,
            buffer_quiet_ms=quiet_ms,manual_confirmation=manual,expected_version=expected_version,
            operation_id=operation_id,expected_selected=selected)

    def live_batch_action(self,pid,sid,bid,revision,operation_id,action):
        self._require_open()
        require(action in ("confirm","cancel"),"invalid_batch_action")
        return self._client.call("POST",f"/v2/batches/{bid}/{action}",
            {"persona_id":pid,"session_id":sid,"expected_revision":revision,"operation_id":operation_id})

    def live_retrieved_notes(self,pid,sid,tid):
        return self._client.manage("desktop.retrieval.notes",pid=pid,sid=sid,tid=tid)

    def live_confirm_inline_main(self,pid,sid,tid,publication,revision,fragments):
        self._require_open()
        return self._client.manage("expression.v2.inline_main",persona_id=pid,session_id=sid,turn_id=tid,
                                   publication_id=publication,revision=revision,fragments=fragments)

    def live_reply_detail(self,pid,sid,tid,kind):
        return self._client.manage("desktop.lookup.detail",pid=pid,sid=sid,tid=tid,kind=kind)

    def live_stage_retry(self,pid,sid,tid,expected,op):
        self._require_open()
        return self._client.manage("desktop.stage.retry",pid=pid,sid=sid,tid=tid,expected=expected,op=op)

    def live_lookup_confirm(self,pid,sid,tid,expected,op,text=None):
        self._require_open()
        return self._client.manage("desktop.lookup.confirm",pid=pid,sid=sid,tid=tid,expected=expected,op=op,text=text)

    def live_location(self,pid=None,sid=None,position=None):
        return self._submissions.location(pid,sid,position)

    def live_submit(self,pid,sid,text,request_id,reference=None,draft_revision=None,public_search=False,input_policy=None,long_requested=False):
        self._require_open()
        if public_search is not False:
            from .private_reply_entry import selected,RESEARCH
            from .knowledge_native import public_task
            chosen=text if public_search is True else public_search
            with Store(self._config.paths).connection() as db:
                research=selected(db,self._config.load()['instance_id'],pid)==RESEARCH
            if not isinstance(chosen,str) or chosen not in text:raise ValueError('native_public_task_scope_changed')
            if not research:public=public_task({'question':chosen,'intent':'search','request_search':True})
        first=not sid
        if not sid:
            sid=self._client.manage("session.select",persona_id=pid,channel="desktop")["session_id"]
            self._submissions.first_session(pid,sid)
        payload = {"persona_id":pid, "session_id":sid, "channel":"desktop", "text":text,
                   "request_id":request_id, "desktop_presentation":CONTRACT}
        if input_policy is not None:
            payload["expected_input_policy"]=input_policy["version"]
        if long_requested:payload['long_requested']=long_requested
        if reference is not None:
            payload["reference"] = reference
        if public_search is not False:
            if research:payload['lookup_span']=chosen
            else:payload['public_task']=public
        result=self._submissions.submit(payload, draft_revision)
        if first and result["state"]=="accepted" and draft_revision:
            empty={"previous":draft_revision,"revision":request_id,"text":"","reference":None}
            self._submissions.draft(pid,sid,empty)
        if result.get("awaiting_confirmation"):
            from .workbench_timeline import input_buffer, require_session
            try:
                with Store(self._config.paths).connection() as db:
                    require_session(db, pid, sid)
                    buffer = input_buffer(db, pid, sid)
                if buffer["batch"] and buffer["batch"]["id"] == result["batch_id"]:
                    result["input_buffer"] = buffer
            except (sqlite3.Error, OSError, RuntimeErrorBase):
                # The accepted message remains in the outbox until the regular refresh catches up.
                pass
        return result|{"session_id":sid}

    def live_submissions(self, pid, sid):
        return self._submissions.reconcile(pid, sid)

    def live_draft(self, pid, sid, draft=None):
        return self._submissions.draft(pid, sid, draft)

    def live_ack_submissions(self, pid, sid, ids):
        self._submissions.acknowledge(pid, sid, ids)

    def live_cancel(self,tid):
        turn=self._client.call("GET","/v1/requests/"+tid)
        return self._client.cancel_request(turn)

    def live_present(self,action,args):
        require(action in ("chat.notice.preview","chat.notice.presented","expression.v2.offer","expression.v2.ack",
                           "expression.v2.pending_receipts"),"不支持的呈现操作。")
        return self._client.manage(action,**args)

    def presentation_attach(self, page):
        self._presentation.attach(page)

    def presentation_begin(self, page, claim_id, kind, args):
        self._require_open()
        return self._presentation.begin(page,claim_id,kind,args)

    def presentation_decide(self, page, claim_id, state, reason=None):
        return self._presentation.decide(page,claim_id,state,reason)

    def presentation_reconcile(self):
        return self._presentation.reconcile()

    def template_spec(self):
        from .models import generic_persona
        return generic_persona().to_dict()

    def live_memory(self,pid,enabled=None,expected=None,expected_provider=None):
        policy=self._client.manage("organization.policy.current",persona_id=pid)
        require(policy["persona_id"]==pid,"人物已切换。")
        config = self._config.load()
        org = config["organization"]
        model = config["model"] if org["reuse_chat"] else org["model"]
        if enabled is not None:
            from .private_personas import provider_identity
            from .models import identifier
            require(type(enabled) is bool and expected==policy["id"],"记忆政策版本已变化。")
            if expected_provider is not None:
                require(expected_provider == {"name":model["name"],"base_url":model["base_url"]}, "接收记忆资料的模型已变化。")
            data=policy["data"] | {"memory_automation":enabled}
            data["provider"] = provider_identity(model)
            if enabled:
                data.update(enabled=True,memory_backfill=True)
            self._use_persona(pid)
            self._client.manage("organization.policy.update",persona_id=pid,expected_selected=pid,
                                expected_version=expected,data=data,operation_id=identifier())
            policy=self._client.manage("organization.policy.current",persona_id=pid)
        return {"enabled":bool(policy["data"]["enabled"] and policy["data"].get("memory_automation")), "version":policy["id"], "provider":{"name":model["name"],"base_url":model["base_url"]}}

    def live_package(self):
        from webview import FileDialog
        files=self._window().create_file_dialog(FileDialog.OPEN,allow_multiple=False,
                                               file_types=("PCAR 角色包 (*.pcarpack;*.json)",))
        return self.read_character_file(files[0]) if files else None

    def read_character_file(self, path):
        from pathlib import Path
        from .character_pack import stage_input, stage_path
        from .character_pack_format import FORMAT, SUFFIX
        from .desktop_character import read_package
        self._require_open()
        if Path(path).suffix.lower() == SUFFIX:
            handle = stage_input(self._config.paths, path)
            try:
                value = self._pack_call('preview_import', handle=handle)
            except BaseException:
                stage_path(self._config.paths, handle).unlink(missing_ok=True)
                raise
            return value | {'handle': handle, 'format': FORMAT}
        data=read_package(path)
        if isinstance(data,dict) and "persona_spec" in data:
            from .character import validate_character
            require(data.get("format")=="pcar_character_v1","角色包格式无效。")
            validate_character(data["character"])
            PersonaSpec.from_dict(data["persona_spec"])
            return data
        PersonaSpec.from_dict(data)
        return {"persona_spec":data}

    def live_save_package(self,package,operation_id):
        from .character_pack_format import FORMAT
        if package.get('format') == FORMAT:
            self._require_open()
            result = self._pack_call('import', handle=package['handle'], digest=package['digest'],
                                     operation_id=operation_id)
            self.live_discard_package(package['handle'])
            self._use_persona(result['id'])
            return result
        if "character" in package:
            selected=next((p["id"] for p in self._client.manage("persona.list") if p["selected"]),None)
            return self._client.manage("character.import",persona_id=selected,package=package,operation_id=operation_id)
        return self.live_create(package["persona_spec"])

    def _pack_call(self, operation, **args):
        return self._client.call('POST', '/v1/manage',
                                 {'action': 'character.pack.' + operation, 'args': args}, timeout=180)

    def live_discard_package(self, handle):
        from .character_pack import stage_path
        stage_path(self._config.paths, handle).unlink(missing_ok=True)
        return True

    def live_package_sessions(self, pid):
        self._require_open()
        return self._pack_call('sessions', persona_id=pid)

    def live_preview_package_export(self, pid, session_ids, main_session):
        self._require_open()
        return self._pack_call('preview_export', persona_id=pid, session_ids=session_ids, main_session=main_session)

    def live_export_package(self, pid, selection=None):
        from pathlib import Path
        from webview import FileDialog
        from .character_pack import save_selected, stage_path
        from .character_pack_format import SUFFIX
        self._require_open()
        suffix = SUFFIX if selection else '.json'
        label = "完整角色包" if selection else "纯角色包"
        files = self._window().create_file_dialog(FileDialog.SAVE, save_filename='PCAR' + suffix,
                                                  file_types=(f"{label} (*{suffix})",))
        if not files:
            return False
        self._require_open()
        target = Path(files if isinstance(files, str) else files[0])
        require(target.suffix.lower() == suffix, "角色包文件扩展名无效。")
        source = self._stage_package_export(pid, selection)
        try:
            save_selected(source, target)
        finally:
            source.unlink(missing_ok=True)
        return True

    def _stage_package_export(self, pid, selection):
        from pathlib import Path
        from .character_pack import stage_path
        from .models import identifier
        if selection is None:
            return Path(self._client.manage('character.export', persona_id=pid, name=identifier() + '.json')['path'])
        require(isinstance(selection, dict) and set(selection) == {'session_ids', 'main_session', 'expected'}
                and isinstance(selection['expected'], str), "请先预览角色包。")
        value = self._pack_call('export', persona_id=pid, **selection)
        return stage_path(self._config.paths, value['handle'])

    def live_create(self,spec):
        person=self._client.manage("persona.create",spec=spec)
        self._client.manage("session.select",persona_id=person["id"],channel="desktop")
        self._use_persona(person["id"])
        return person

    def live_model(self,name,key):
        require(name in ("deepseek-v4-pro","deepseek-flash"),"模型选项无效。")
        model={"base_url":"https://api.deepseek.com","name":name}
        if key:
            model["api_key"]=key
        self._client.call("POST","/v1/config",{"model":model})
        return True
