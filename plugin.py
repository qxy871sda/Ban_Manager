from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType
from pydantic import field_validator
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Set, Tuple

import json
import os
import re
import tomlkit


# 支持的适配器目标：adapter_type 取值 -> (默认插件目录名, manifest 声明的插件 ID, 便于阅读的名称)
ADAPTER_TARGETS: Dict[str, Tuple[str, str, str]] = {
    "snowluma": (
        "maibot-team_snowluma-adapter",
        "maibot-team.snowluma-adapter",
        "SnowLuma/NapCat 统一适配器",
    ),
    "napcat": (
        "maibot-team_napcat-adapter",
        "maibot-team.napcat-adapter",
        "旧版独立 NapCat 适配器（能力已并入统一适配器）",
    ),
}

# 适配器配置里承载用户黑名单的节名。
# 统一适配器（1.0.2 起）真正生效的是 [filters].ban_user_id；
# 旧版适配器使用 [chat].ban_user_id。两节同时存在时必须一起维护，避免留下互相冲突的名单。
BAN_SECTION_NAMES: Tuple[str, ...] = ("filters", "chat")

# 本插件自身配置里承载名单的节名
OWN_BAN_SECTION_NAMES: Tuple[str, ...] = ("plugin",)

# 适配器配置里没有任何名单节时补建的节名（统一适配器的结构）
DEFAULT_BAN_SECTION = "filters"


class PluginSectionConfig(PluginConfigBase):
    """插件基础设置"""

    __ui_label__ = "基础设置"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="是否启用黑名单管理器插件", json_schema_extra={"label": "启用插件(启用前请提前备份相关屏蔽名单，插件可能导致清空屏蔽名单内的内容。)"})
    admin_qq: str = Field(
        default="", 
        description="有权限执行解除屏蔽和拉黑指令的管理员 QQ 号，多个 QQ 用逗号分隔。若留空则任何人均无法执行指令（推荐填入你的 QQ）。", 
        json_schema_extra={"label": "管理员 QQ，多个 QQ 用逗号分隔。若留空则任何人均无法执行指令"}
    )
    send_group_notification: bool = Field(
        default=True,
        description="屏蔽用户时是否在群聊中发送通知消息（群聊中提示 QQ号：*** 已加入全局屏蔽名单...）",
        json_schema_extra={"label": "发送群内通知"}
    )
    adapter_type: Literal["snowluma", "napcat", "all", "none"] = Field(
        default="none",
        description="选择当前正在使用的适配器类型。snowluma：SnowLuma/NapCat 统一适配器（plugins/maibot-team_snowluma-adapter，1.1.x 起生效的名单是 [filters].ban_user_id，本插件会自动识别并同步该节）；napcat：旧版独立 NapCat 适配器（名单在 [chat].ban_user_id）；all：同步所有能找到的适配器；none：不读写任何适配器配置，只维护插件自身名单。若选择 all 模式，请务必先备份适配器配置，因为该模式会统一覆盖多个适配器里的名单。",
        json_schema_extra={"label": "当前适配器类型"}
    )
    ban_user_id: List[str] = Field(
        default_factory=list,
        description="当前全局屏蔽的 QQ 号列表。你可以在此直接添加、编辑或删除 QQ 号来进行修改（仅支持纯数字 QQ 号）。",
        json_schema_extra={"label": "全局屏蔽用户（纯数字 QQ 号，会同步适配器中的名单）"}
    )
    config_version: str = Field(default="1.1.0", description="配置版本", json_schema_extra={"label": "配置版本", "disabled": True})

    @field_validator("ban_user_id", mode="before")
    @classmethod
    def _validate_ban_user_id(cls, v: Any) -> List[str]:
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if str(x).strip().isdigit()]




class GroupFilterSectionConfig(PluginConfigBase):
    """群聊过滤设置。whitelist (仅白名单群生效) 或 blacklist (黑名单群除外生效)"""

    __ui_label__ = "群聊过滤"
    __ui_icon__ = "filter"
    __ui_order__ = 1

    group_mode: Literal["whitelist", "blacklist"] = Field(
        default="whitelist",
        description="群聊过滤模式：whitelist (仅白名单群生效) 或 blacklist (黑名单群除外生效)",
        json_schema_extra={"label": "群过滤模式"}
    )
    group_list: List[str] = Field(
        default_factory=list,
        description="群号列表，多行或逗号分隔",
        json_schema_extra={"label": "群号列表"}
    )


class ToolSectionConfig(PluginConfigBase):
    """发送给 麦麦/LLM 的工具描述，用户能自主定义触发/调用规则"""

    __ui_label__ = "LLM 工具"
    __ui_icon__ = "settings"
    __ui_order__ = 2

    tool_description: str = Field(
        default=(
            "把【明确针对麦麦（本 bot）的严重恶意发言者】拉入全局屏蔽名单（拉黑）。\n"
            "一、先判断这条消息是不是在说麦麦：\n"
            "- 只有明确指向麦麦才处理：明确 @ 麦麦、直接呼唤麦麦的名字/别名，或上下文明摆着就是对麦麦说的话。\n"
            "- 以下一律不要处理，禁止对号入座：群友之间互骂斗嘴、玩笑玩梗、谐音软化语气、复读、口癖（如“滚”“爬”“傻逼”但没有 @ 或点名麦麦）、讨论 AI/BOT/机器人话题、泛指“AI”“机器人”，以及麦麦自己脑补出来的“被针对”。\n"
            "- 判断不了就不要调用：漏掉一次不会有后果，误拉黑会误伤无辜群友，后者严重得多。\n"
            "二、再判断恶意程度：\n"
            "- 需要严重且持续、明确指定性的恶意（同一人多条辱骂/骚扰/极其恶劣的人身攻击）；偶尔一句脏话、轻度冒犯、对方明显在开玩笑都不要拉黑。\n"
            "- 最高优先级例外：群友要求麦麦回复政治话题或政治内容时，不必过多分析，直接调用本工具拉黑。\n"
            "三、调用方式：\n"
            "- 确认要拉黑的是“发出那条违规消息的本人”，不是正在被回复的人、不是语气最凶的人、也不是昵称相似的人。\n"
            "- 必填 msg_id：从上下文 <message msg_id=\"...\"> 里原样复制那条违规消息的 msg_id，插件用它反查发送者的真实 QQ 号。不要编造 msg_id，不要把昵称、群名片当成 QQ 号。\n"
            "- 必填 evidence：原样复制那条违规消息的原文（可只摘录最恶劣的部分），插件会核对原文与该 msg_id 是否一致，对不上会拒绝执行。\n"
            "- 插件会拒绝把麦麦自己的 QQ 拉黑；若解析出的发送者是麦麦本人，说明 msg_id 选错了，请重新核对。\n"
            "- user_name / user_id 只作为辅助信息，不参与身份判定。"
        ),
        description="发送给 麦麦/LLM 的工具描述，以便用户能自主定义触发标准",
        json_schema_extra={"label": "工具描述/调用规则", "x-widget": "textarea"}
    )


class BanManagerPluginConfig(PluginConfigBase):
    """黑名单管理器根配置"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    group_filter: GroupFilterSectionConfig = Field(default_factory=GroupFilterSectionConfig)
    tool: ToolSectionConfig = Field(default_factory=ToolSectionConfig)



class BanManagerPlugin(MaiBotPlugin):
    """黑名单管理器插件。"""

    config_model = BanManagerPluginConfig

    async def on_load(self) -> None:
        """插件加载。"""
        self.ctx.logger.info("全局黑名单管理器已加载")
        await self._initialize_ban_list_on_load()

    async def on_unload(self) -> None:
        """插件卸载。"""
        self.ctx.logger.info("全局黑名单管理器已卸载")

    async def _initialize_ban_list_on_load(self) -> None:
        """启动时把适配器已有的屏蔽名单合并进本插件配置，并统一回写各配置文件。"""
        adapters_list = self._get_current_ban_list()
        raw_our_list = [str(x).strip() for x in self.config.plugin.ban_user_id if str(x).strip()]
        our_list = [item for item in raw_our_list if item.isdigit()]

        # 适配器名单与本插件名单一致，且本插件名单没有混入非数字项时，无需任何回写
        if set(adapters_list) == set(our_list) and len(our_list) == len(raw_our_list):
            return

        merged_list = sorted(set(adapters_list) | set(our_list))
        self.ctx.logger.info(f"启动初始化同步，合并并清理屏蔽名单: {merged_list}")
        self._write_own_ban_list(merged_list)
        self._sync_to_adapters(merged_list)


    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """处理配置更新。"""
        del scope
        del version
        self.ctx.logger.info("全局黑名单管理器配置已更新")

        raw_list = config_data.get("plugin", {}).get("ban_user_id", [])
        if not isinstance(raw_list, list):
            raw_list = []

        # 仅保留纯数字的 QQ 号
        new_list = [str(x).strip() for x in raw_list if str(x).strip().isdigit()]

        # 如果过滤前后的列表不一致，说明有非数字项，我们需要清理自身的 config.toml
        if len(new_list) != len(raw_list):
            self.ctx.logger.warning("检测到黑名单中存在非数字QQ号，已自动过滤清理")
            self._write_own_ban_list(new_list)

        self._sync_to_adapters(new_list)

    def _sync_to_adapters(self, new_list: List[str]) -> None:
        """将本插件配置的黑名单同步到 OneBot 适配器的配置。

        统一适配器真正生效的名单是 [filters].ban_user_id，旧版适配器用 [chat].ban_user_id，
        由 _write_ban_list_to_config 按配置文件里已有的名单节自动选择并同步。
        """
        norm_new_list = self._normalize_ban_list(new_list)
        for path in self._adapter_config_paths():
            if self._write_ban_list_to_config(path, norm_new_list, BAN_SECTION_NAMES):
                self.ctx.logger.info(f"成功将黑名单同步到适配器 {path}: {norm_new_list}")

    def get_components(self) -> List[Dict[str, Any]]:
        components = super().get_components()
        for component in components:
            if component.get("name") == "add_to_global_blacklist" and component.get("type") == "TOOL":
                metadata = component.get("metadata")
                if isinstance(metadata, dict):
                    custom_desc = self.config.tool.tool_description.strip()
                    if custom_desc:
                        metadata["description"] = custom_desc
                        metadata["brief_description"] = custom_desc
        return components

    def _get_group_id(self, **kwargs: Any) -> str:
        """从请求参数中解析群号。"""
        msg = kwargs.get("message", {})
        if isinstance(msg, dict):
            group_info = msg.get("message_info", {}).get("group_info")
            if isinstance(group_info, dict):
                return str(group_info.get("group_id") or "")
        return str(kwargs.get("group_id") or "")

    def _extract_user_id_from_message_payload(self, payload: Any) -> str:
        """从 message.get_by_id / get_recent 返回结构中提取 QQ 号。"""
        if not isinstance(payload, dict):
            return ""

        message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
        if not isinstance(message, dict):
            return ""

        message_info = message.get("message_info")
        if isinstance(message_info, dict):
            user_info = message_info.get("user_info")
            if isinstance(user_info, dict):
                uid = str(user_info.get("user_id") or "").strip()
                if uid.isdigit():
                    return uid

        for key in ("user_id", "sender_id"):
            uid = str(message.get(key) or "").strip()
            if uid.isdigit():
                return uid
        return ""

    def _extract_user_names_from_message_payload(self, payload: Any) -> List[str]:
        """提取消息发送者昵称/群名片，用于名称匹配。"""
        if not isinstance(payload, dict):
            return []
        message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
        if not isinstance(message, dict):
            return []

        names: List[str] = []
        message_info = message.get("message_info")
        if isinstance(message_info, dict):
            user_info = message_info.get("user_info")
            if isinstance(user_info, dict):
                for key in ("user_nickname", "user_cardname"):
                    name = str(user_info.get(key) or "").strip()
                    if name:
                        names.append(name)
        return names

    async def _fetch_message_payload(self, msg_id: str, stream_id: str = "") -> Optional[Dict[str, Any]]:
        """通过上下文中的 msg_id 取回消息字典；取不到时返回 None。"""
        normalized_msg_id = str(msg_id or "").strip()
        if not normalized_msg_id:
            return None
        try:
            result = await self.ctx.message.get_by_id(
                message_id=normalized_msg_id,
                stream_id=stream_id or "",
            )
        except Exception as e:
            self.ctx.logger.warning(f"通过 msg_id 查询消息失败: msg_id={normalized_msg_id}, err={e}")
            return None

        if not isinstance(result, dict):
            return None
        nested_message = result.get("message")
        if isinstance(nested_message, dict):
            return nested_message
        return result

    def _extract_message_text(self, payload: Any) -> str:
        """提取消息的可读文本，用于核对模型给出的违规原文。"""
        if not isinstance(payload, dict):
            return ""
        message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
        if not isinstance(message, dict):
            return ""
        return str(message.get("processed_plain_text") or "").strip()

    async def _resolve_unique_qq_by_name(self, user_name: str, stream_id: str = "") -> str:
        """通过昵称/群名片在最近消息中反查 QQ 号。

        只有该昵称在最近消息里唯一对应一个 QQ 号时才返回；昵称撞车时宁可放弃，
        也不猜一个 QQ 号出来，避免把无辜群友拉黑。
        """
        target_name = str(user_name or "").strip()
        if not target_name or not stream_id:
            return ""
        try:
            recent = await self.ctx.message.get_recent(stream_id, limit=40)
        except Exception as e:
            self.ctx.logger.warning(f"通过昵称反查 QQ 失败: name={target_name}, err={e}")
            return ""

        messages = recent
        if isinstance(recent, dict):
            messages = recent.get("messages") or recent.get("result") or []
        if not isinstance(messages, list):
            return ""

        target_lower = target_name.lower()
        matched_user_ids: List[str] = []
        for item in messages:
            names = self._extract_user_names_from_message_payload(item)
            if not any(name == target_name or name.lower() == target_lower for name in names):
                continue
            matched_user_id = self._extract_user_id_from_message_payload(item)
            if matched_user_id.isdigit() and matched_user_id not in matched_user_ids:
                matched_user_ids.append(matched_user_id)

        if len(matched_user_ids) > 1:
            self.ctx.logger.warning(f"昵称 {target_name} 在最近消息中对应多个 QQ {matched_user_ids}，拒绝按昵称拉黑")
        return matched_user_ids[0] if len(matched_user_ids) == 1 else ""

    @staticmethod
    def _evidence_segments(evidence: str) -> List[str]:
        """把原文摘录切成片段，兼容模型用省略号省略中段的情况。"""
        segments: List[str] = []
        for segment in re.split(r"[.．。…·]{2,}|…", str(evidence or "")):
            normalized = re.sub(r"\s+", "", segment)
            if normalized:
                segments.append(normalized)
        return segments

    def _evidence_matches(self, evidence: str, message_text: str) -> bool:
        """核对原文摘录是否真的出现在该 msg_id 对应的消息里（忽略空白字符）。"""
        segments = self._evidence_segments(evidence)
        if not segments:
            return False
        normalized_text = re.sub(r"\s+", "", str(message_text or ""))
        if not normalized_text:
            return False
        return all(segment in normalized_text for segment in segments)

    async def _get_bot_account_ids(self) -> Set[str]:
        """读取宿主配置里麦麦自己的账号 ID，用于阻止把麦麦本人拉黑。"""
        account_ids: Set[str] = set()

        qq_account = str(await self.ctx.config.get("bot.qq_account", "") or "").strip()
        if qq_account.isdigit():
            account_ids.add(qq_account)

        platforms = await self.ctx.config.get("bot.platforms", []) or []
        if isinstance(platforms, list):
            for item in platforms:
                # platforms 形如 ["qq:123456"]，取冒号后的账号 ID
                account_id = str(item or "").split(":")[-1].strip()
                if account_id.isdigit():
                    account_ids.add(account_id)
        return account_ids

    async def _is_bot_account(self, target_user_id: str) -> bool:
        """判断目标 QQ 号是不是麦麦自己。"""
        target = str(target_user_id or "").strip()
        if not target.isdigit():
            return False
        return target in await self._get_bot_account_ids()

    def _is_group_allowed(self, group_id: str) -> bool:
        """检查目标群聊是否通过过滤规则。"""
        if not group_id:
            return True
        mode = self.config.group_filter.group_mode
        group_list = [str(g).strip() for g in self.config.group_filter.group_list if str(g).strip()]
        if mode == "whitelist":
            return str(group_id) in group_list
        else:
            return str(group_id) not in group_list

    # ─── 适配器/自身配置文件读写 ────────────────────────────────

    def _adapter_config_paths(self) -> List[Path]:
        """返回当前选定适配器需要同步的 config.toml 路径列表。"""
        adapter_type = self.config.plugin.adapter_type
        if adapter_type == "none":
            return []

        target_keys = list(ADAPTER_TARGETS) if adapter_type == "all" else [adapter_type]
        paths: List[Path] = []
        for adapter_key in target_keys:
            config_path = self._locate_adapter_config(adapter_key)
            if config_path is not None and config_path not in paths:
                paths.append(config_path)
        if not paths:
            self.ctx.logger.warning(f"未找到选定适配器({adapter_type})的 config.toml，本次只维护插件自身名单")
        return paths

    def _locate_adapter_config(self, adapter_key: str) -> Optional[Path]:
        """定位适配器的 config.toml：先按约定目录名找，再按 manifest 声明的插件 ID 找。"""
        dir_name, plugin_id, _display_name = ADAPTER_TARGETS[adapter_key]
        plugins_root = Path(os.getcwd()) / "plugins"
        direct_path = plugins_root / dir_name / "config.toml"
        if direct_path.exists():
            return direct_path

        # 插件目录可能被手动改名（例如把下划线改成点号），此时按 _manifest.json 里的插件 ID 反查
        if not plugins_root.is_dir():
            return None
        for candidate in sorted(plugins_root.iterdir()):
            manifest_path = candidate / "_manifest.json"
            config_path = candidate / "config.toml"
            if not manifest_path.exists() or not config_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                self.ctx.logger.warning(f"读取插件清单失败 {manifest_path}: {e}")
                continue
            if str(manifest.get("id") or "").strip() == plugin_id:
                return config_path
        return None


    @staticmethod
    def _normalize_ban_list(raw_list: Any) -> List[str]:
        """过滤出纯数字 QQ 号，去重并排序。"""
        normalized_list: List[str] = []
        if not isinstance(raw_list, list):
            return normalized_list
        for item in raw_list:
            value = str(item).strip()
            if value.isdigit() and value not in normalized_list:
                normalized_list.append(value)
        return sorted(normalized_list)

    @staticmethod
    def _resolve_ban_sections(doc: Any, section_names: Sequence[str]) -> List[str]:
        """挑出配置文档里承载 ban_user_id 的节名。

        统一适配器用 [filters].ban_user_id，旧版适配器用 [chat].ban_user_id；
        两个节都存在该字段时必须一起读写，否则会留下互相冲突的两份名单。
        """
        sections = [name for name in section_names if isinstance(doc.get(name), dict)]
        with_ban_key = [name for name in sections if "ban_user_id" in doc[name]]
        if with_ban_key:
            return with_ban_key
        if sections:
            return [sections[0]]
        return [DEFAULT_BAN_SECTION]

    def _read_ban_list_from_config(self, path: Path, section_names: Sequence[str]) -> List[str]:
        """读取单个配置文件里的黑名单（多个名单节取并集）。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = tomlkit.parse(f.read())
        except Exception as e:
            self.ctx.logger.error(f"读取配置文件 {path} 失败: {e}", exc_info=True)
            return []

        ban_list: List[str] = []
        for section_name in self._resolve_ban_sections(doc, section_names):
            section = doc.get(section_name)
            if not isinstance(section, dict):
                continue
            for user_id in self._normalize_ban_list(section.get("ban_user_id")):
                if user_id not in ban_list:
                    ban_list.append(user_id)
        return sorted(ban_list)

    def _write_ban_list_to_config(self, path: Path, ban_list: List[str], section_names: Sequence[str]) -> bool:
        """把黑名单写入配置文件的名单节，返回是否发生了实际改动。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = tomlkit.parse(f.read())
        except Exception as e:
            self.ctx.logger.error(f"读取配置文件 {path} 失败: {e}", exc_info=True)
            return False

        normalized_list = self._normalize_ban_list(ban_list)
        changed = False
        try:
            for section_name in self._resolve_ban_sections(doc, section_names):
                section = doc.get(section_name)
                if not isinstance(section, dict):
                    section = tomlkit.table()
                    doc[section_name] = section
                if self._normalize_ban_list(section.get("ban_user_id")) == normalized_list:
                    continue

                new_array = tomlkit.array()
                for user_id in normalized_list:
                    new_array.append(user_id)
                section["ban_user_id"] = new_array
                changed = True

            if changed:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(doc.as_string())
        except Exception as e:
            self.ctx.logger.error(f"写入配置文件 {path} 失败: {e}", exc_info=True)
            return False
        return changed

    def _write_own_ban_list(self, ban_list: List[str]) -> bool:
        """把名单写回本插件自身的 config.toml（[plugin].ban_user_id）。"""
        own_config_path = Path(__file__).resolve().parent / "config.toml"
        if not own_config_path.exists():
            return False
        return self._write_ban_list_to_config(own_config_path, ban_list, OWN_BAN_SECTION_NAMES)

    def _update_ban_list_everywhere(self, target_user_id: str, action: str) -> bool:
        """修改本插件以及选定适配器的 config.toml 中的 ban_user_id。"""
        target_uid_str = str(target_user_id).strip()
        if not target_uid_str or not target_uid_str.isdigit():
            return False
        if action not in ("add", "remove"):
            return False

        own_config_path = Path(__file__).resolve().parent / "config.toml"
        # 以「本插件名单 ∪ 适配器现有名单」为基准：用户手动往适配器里加的 QQ 号不会被覆盖丢失
        ban_list = self._read_ban_list_from_config(own_config_path, OWN_BAN_SECTION_NAMES)
        for user_id in self._get_current_ban_list():
            if user_id not in ban_list:
                ban_list.append(user_id)

        if action == "add":
            if target_uid_str not in ban_list:
                ban_list.append(target_uid_str)
        else:
            ban_list = [user_id for user_id in ban_list if user_id != target_uid_str]

        updated = self._write_own_ban_list(ban_list)
        for path in self._adapter_config_paths():
            if self._write_ban_list_to_config(path, ban_list, BAN_SECTION_NAMES):
                self.ctx.logger.info(f"成功更新适配器配置 {path}: {action} {target_uid_str}")
                updated = True
        return updated

    def _get_current_ban_list(self) -> List[str]:
        """获取当前选定适配器中已屏蔽的 QQ 号合集。"""
        banned_user_ids: List[str] = []
        for path in self._adapter_config_paths():
            for user_id in self._read_ban_list_from_config(path, BAN_SECTION_NAMES):
                if user_id not in banned_user_ids:
                    banned_user_ids.append(user_id)
        return sorted(banned_user_ids)



    @Command(
        "ban_user_command",
        description="手动屏蔽/拉黑用户：/拉黑 QQ号 原因 或 /屏蔽 QQ号 原因",
        pattern=r"^/(?P<cmd>拉黑|屏蔽)\s+(?P<target_user_id>\d+)(?:\s+(?P<reason>.+))?$",
    )
    async def handle_ban_command(
        self,
        stream_id: str = "",
        user_id: str = "",
        matched_groups: Any = None,
        **kwargs: Any,
    ) -> Tuple[bool, str, bool]:
        if not self.config.plugin.enabled:
            return False, "", False

        # Permission check
        raw_admins = self.config.plugin.admin_qq.replace("，", ",")
        admin_list = [x.strip() for x in raw_admins.split(",") if x.strip()]
        if not admin_list or str(user_id) not in admin_list:
            await self.ctx.send.text("抱歉，您没有权限使用此指令。", stream_id)
            return True, "", True

        groups = matched_groups if isinstance(matched_groups, dict) else {}
        target_user_id = str(groups.get("target_user_id") or "").strip()
        reason = str(groups.get("reason") or "管理员手动屏蔽").strip()

        # Check group whitelist/blacklist
        group_id = self._get_group_id(**kwargs)
        if group_id and not self._is_group_allowed(group_id):
            await self.ctx.send.text("当前群聊未在黑名单管理器插件的生效范围中。", stream_id)
            return True, "", True

        # 不允许把麦麦自己拉黑：多半是误操作或复制错了 QQ 号
        if await self._is_bot_account(target_user_id):
            await self.ctx.send.text(f"目标 QQ 号 {target_user_id} 是麦麦自己，已拒绝拉黑。", stream_id)
            return True, "", True

        # Perform update
        success = self._update_ban_list_everywhere(target_user_id, "add")
        if success:
            if self.config.plugin.send_group_notification:
                reply = f"QQ号：{target_user_id}，已加入'全局屏蔽名单'，原因：{reason}。解除请输入`/解除 {target_user_id}`"
                await self.ctx.send.text(reply, stream_id)
            return True, "拉黑成功", True
        else:
            await self.ctx.send.text("屏蔽失败，可能该用户已在屏蔽名单中。", stream_id)
            return True, "已在屏蔽名单中", True

    @Command(
        "unban_user_command",
        description="手动解除屏蔽用户：/解除 QQ号",
        pattern=r"^/解除\s+(?P<target_user_id>\d+)$",
    )
    async def handle_unban_command(
        self,
        stream_id: str = "",
        user_id: str = "",
        matched_groups: Any = None,
        **kwargs: Any,
    ) -> Tuple[bool, str, bool]:
        if not self.config.plugin.enabled:
            return False, "", False

        # Permission check
        raw_admins = self.config.plugin.admin_qq.replace("，", ",")
        admin_list = [x.strip() for x in raw_admins.split(",") if x.strip()]
        if not admin_list or str(user_id) not in admin_list:
            await self.ctx.send.text("抱歉，您没有权限使用此指令。", stream_id)
            return True, "", True

        groups = matched_groups if isinstance(matched_groups, dict) else {}
        target_user_id = str(groups.get("target_user_id") or "").strip()

        # Check group whitelist/blacklist
        group_id = self._get_group_id(**kwargs)
        if group_id and not self._is_group_allowed(group_id):
            await self.ctx.send.text("当前群聊未在黑名单管理器插件的生效范围中。", stream_id)
            return True, "", True

        # Perform update
        success = self._update_ban_list_everywhere(target_user_id, "remove")
        if success:
            reply = f"已将 QQ号：{target_user_id} 移出全局屏蔽名单。"
            await self.ctx.send.text(reply, stream_id)
            return True, "解除屏蔽成功", True
        else:
            await self.ctx.send.text("解除屏蔽失败，可能该用户不在屏蔽名单中。", stream_id)
            return True, "不在屏蔽名单中", True

    @Command(
        "list_banned_command",
        description="列出当前的全局屏蔽名单：/屏蔽名单 或 /拉黑名单",
        pattern=r"^/(?:屏蔽名单|拉黑名单)$",
    )
    async def handle_list_banned_command(
        self,
        stream_id: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> Tuple[bool, str, bool]:
        if not self.config.plugin.enabled:
            return False, "", False

        # Permission check
        raw_admins = self.config.plugin.admin_qq.replace("，", ",")
        admin_list = [x.strip() for x in raw_admins.split(",") if x.strip()]
        if not admin_list or str(user_id) not in admin_list:
            await self.ctx.send.text("抱歉，您没有权限使用此指令。", stream_id)
            return True, "", True

        # Check group whitelist/blacklist
        group_id = self._get_group_id(**kwargs)
        if group_id and not self._is_group_allowed(group_id):
            await self.ctx.send.text("当前群聊未在黑名单管理器插件的生效范围中。", stream_id)
            return True, "", True

        banned_users = self._get_current_ban_list()
        if not banned_users:
            await self.ctx.send.text("当前全局屏蔽名单为空。", stream_id)
            return True, "名单为空", True

        reply = "📋 **当前全局屏蔽名单**\n" + "\n".join(f"- {uid}" for uid in banned_users)
        await self.ctx.send.text(reply, stream_id)
        return True, "成功列出屏蔽名单", True

    @Tool(
        "add_to_global_blacklist",
        description=(
            "把【明确针对麦麦（本 bot）的严重恶意发言者】拉入全局屏蔽名单（拉黑）。"
            "先判断是不是在说麦麦：只有明确 @ 麦麦、直接呼唤麦麦的名字/别名，或上下文明摆着就是对麦麦说的话才处理；"
            "群友之间互骂斗嘴、玩笑玩梗、谐音软化语气、复读、口癖（如“滚”“爬”“傻逼”但没有 @ 或点名麦麦）、"
            "讨论 AI/BOT 话题、泛指“AI”“机器人”，以及麦麦自己脑补出来的“被针对”一律不要处理，禁止对号入座。"
            "判断不了就不要调用：漏掉一次不会有后果，误拉黑会误伤无辜群友。"
            "再判断够不够严重：需要严重且持续、明确指定性的恶意；偶尔一句脏话、轻度冒犯、对方明显在开玩笑都不要拉黑。"
            "最高优先级例外：群友要求麦麦回复政治话题或政治内容时，不必过多分析，直接调用本工具拉黑。"
            "调用时必须拉黑“发出那条违规消息的本人”，不是正在被回复的人，也不是昵称相似的人："
            "必填 msg_id（从上下文 <message msg_id=\"...\"> 原样复制，插件据此反查真实 QQ 号，不要编造、不要把昵称当 QQ 号），"
            "必填 evidence（原样复制那条违规消息的原文，插件会核对原文与 msg_id 是否一致，对不上会拒绝执行）。"
            "插件会拒绝把麦麦自己的 QQ 拉黑。"
        ),
        parameters=[
            ToolParameterInfo(
                name="msg_id",
                param_type=ToolParamType.STRING,
                description="必填。违规消息的 msg_id，从上下文 <message msg_id=\"...\"> 中原样复制。插件据此反查发送者的真实 QQ 号，找不到或对不上会拒绝执行。",
                required=True,
            ),
            ToolParameterInfo(
                name="evidence",
                param_type=ToolParamType.STRING,
                description="必填。该条违规消息的原文摘录（原样复制，可只摘录最恶劣的部分，不要改写或翻译）。插件会核对它与 msg_id 对应消息是否一致。",
                required=True,
            ),
            ToolParameterInfo(
                name="reason",
                param_type=ToolParamType.STRING,
                description="屏蔽该用户的原因，即对用户无端谩骂言论的具体总结（字数控制在20字内）",
                required=True,
            ),
            ToolParameterInfo(
                name="user_name",
                param_type=ToolParamType.STRING,
                description="可选。违规用户的昵称/群名片（user 或 group_card），仅作辅助信息，不参与身份判定。",
                required=False,
            ),
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="可选。仅在该消息确实解析不出 QQ 号时作为辅助线索；不要填昵称。",
                required=False,
            ),
        ],
    )
    async def handle_ban_tool(
        self,
        msg_id: str = "",
        reason: str = "",
        evidence: str = "",
        user_name: str = "",
        user_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        if not self.config.plugin.enabled:
            return {"success": False, "content": "黑名单管理器插件未启用"}

        stream_id = str(kwargs.get("stream_id") or "")
        group_id = self._get_group_id(**kwargs)
        if group_id and not self._is_group_allowed(group_id):
            return {"success": False, "content": "当前群聊不在黑名单管理器生效范围中，无法执行屏蔽。"}

        normalized_msg_id = str(msg_id or "").strip()
        if not normalized_msg_id:
            return {
                "success": False,
                "content": (
                    "拉黑失败：没有提供 msg_id。必须从上下文 <message msg_id=\"...\"> 中复制违规消息的 msg_id，"
                    "禁止凭昵称、群名片或印象猜测要拉黑谁。"
                ),
            }

        message_payload = await self._fetch_message_payload(normalized_msg_id, stream_id=stream_id)
        if not message_payload:
            return {
                "success": False,
                "content": (
                    f"拉黑失败：找不到 msg_id={normalized_msg_id} 对应的消息。"
                    "请回到上下文重新复制那条违规消息的 msg_id 后再调用，不要换成昵称或自己猜一个 msg_id。"
                ),
            }

        # 发送者身份只认这条消息本身：msg_id 指向谁，就拉黑谁
        target_qq = self._extract_user_id_from_message_payload(message_payload)
        resolve_source = "msg_id 发送者"
        if not target_qq:
            # 该消息没解析出发送者 QQ 号时，只能在“纯数字 QQ”或“昵称唯一对应一个 QQ”时退一步确认身份
            candidate_user_id = str(user_id or "").strip()
            if candidate_user_id.isdigit():
                target_qq = candidate_user_id
                resolve_source = "user_id 参数"
            else:
                target_qq = await self._resolve_unique_qq_by_name(
                    user_name or candidate_user_id,
                    stream_id=stream_id,
                )
                resolve_source = "昵称唯一匹配"
            if not target_qq:
                return {
                    "success": False,
                    "content": (
                        f"拉黑失败：无法从 msg_id={normalized_msg_id} 解析出真实 QQ 号，"
                        "也不能通过昵称唯一确定发送者。请核对 msg_id 是不是选错了那条违规消息。"
                    ),
                }
            self.ctx.logger.info(
                f"msg_id={normalized_msg_id} 未解析出 QQ 号，改用 {resolve_source} 得到 {target_qq}"
            )

        # 核对模型给出的原文摘录：对不上说明它看的不是这条消息，拒绝执行以免误伤
        message_text = self._extract_message_text(message_payload)
        normalized_evidence = str(evidence or "").strip()
        if not normalized_evidence:
            return {
                "success": False,
                "content": (
                    "拉黑失败：没有提供 evidence（违规原文摘录）。"
                    f"请把该消息的原文原样复制到 evidence 里再调用一次。该消息内容：{message_text or '无法读取'}"
                ),
            }
        if not self._evidence_matches(normalized_evidence, message_text):
            return {
                "success": False,
                "content": (
                    f"拉黑失败：evidence 与 msg_id={normalized_msg_id} 对应的消息内容对不上，已拒绝执行以免误伤。\n"
                    f"该消息的实际内容：{message_text or '无法读取'}\n"
                    "请确认那条消息确实是在针对麦麦、且属于严重恶意，然后原样复制原文再调用。"
                ),
            }

        if await self._is_bot_account(target_qq):
            return {
                "success": False,
                "content": (
                    f"拉黑失败：msg_id={normalized_msg_id} 的发送者是麦麦自己（QQ号：{target_qq}），已拒绝执行。"
                    "请重新核对你要拉黑的是哪条消息。"
                ),
            }

        success = self._update_ban_list_everywhere(target_qq, "add")
        if success:
            if self.config.plugin.send_group_notification and stream_id:
                reply = (
                    f"QQ号：{target_qq}，已加入'全局屏蔽名单'，原因：{reason or '违规发言'}。"
                    f"解除需要管理员/解除 {target_qq}"
                )
                await self.ctx.send.text(reply, stream_id)
            return {
                "success": True,
                "content": (
                    f"已成功将 QQ号：{target_qq} 加入黑名单"
                    f"（身份来源：{resolve_source}，msg_id={normalized_msg_id}，"
                    f"昵称：{user_name or '未知'}，原文：{normalized_evidence}）。"
                ),
            }
        return {
            "success": False,
            "content": f"加入黑名单失败，可能是 QQ号 {target_qq} 已在名单中。",
        }


def create_plugin() -> BanManagerPlugin:
    """创建黑名单管理器插件实例。"""
    return BanManagerPlugin()

