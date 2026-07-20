from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType
from pydantic import field_validator
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Literal, Optional, Tuple

import asyncio
import os
import tomlkit


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
        description="选择当前正在使用的适配器类型。all 模式会尝试同步所有支持的适配器。none 模式将不会读取或修改任何适配器的配置文件（仅维护自身黑名单）。若选择 all 模式，请务必先备份适配器配置，因为该模式可能会覆盖不同适配器间的名单。",
        json_schema_extra={"label": "当前适配器类型"}
    )
    ban_user_id: List[str] = Field(
        default_factory=list,
        description="当前全局屏蔽的 QQ 号列表。你可以在此直接添加、编辑或删除 QQ 号来进行修改（仅支持纯数字 QQ 号）。",
        json_schema_extra={"label": "全局屏蔽用户（纯数字 QQ 号，会同步适配器中的名单）"}
    )
    config_version: str = Field(default="1.0.0", description="配置版本", json_schema_extra={"label": "配置版本", "disabled": True})

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
            "当检测到群友对 bot 的发言中包含无端谩骂/骚扰、极其恶劣的人身攻击等行为时，"
            "调用此工具将该用户拉入全局屏蔽名单（拉黑）。"
            "重要：上下文消息形如 <message msg_id=\"...\" user=\"昵称\" group_card=\"群名片\">，"
            "其中没有直接给出 QQ 号。"
            "调用时必须优先填写违规消息的 msg_id（从消息标签 msg_id 属性复制），"
            "可附带 user 昵称；插件会自动把 msg_id 反查为真实 QQ 号后加入黑名单。"
            "禁止把昵称、群名片当成 QQ 号传入。"
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
        """启动时同步适配器已有的屏蔽名单到本插件的配置中。"""
        adapters_list = [str(x).strip() for x in self._get_current_ban_list() if str(x).strip().isdigit()]
        our_list = [str(x).strip() for x in self.config.plugin.ban_user_id if str(x).strip().isdigit()]
        raw_our_list = [str(x).strip() for x in self.config.plugin.ban_user_id if str(x).strip()]
        
        # 如果适配器中有，但本插件配置中没有，则进行合并同步；或者配置中包含非数字字段，我们需要清理
        if set(adapters_list) != set(our_list) or len(our_list) != len(raw_our_list):
            merged_list = sorted(list(set(adapters_list) | set(our_list)))
            self.ctx.logger.info(f"启动初始化同步，合并并清理屏蔽名单: {merged_list}")
            
            # 写入本插件及所有适配器的配置文件
            import tomlkit
            workspace_root = Path(os.getcwd())
            snowluma_config_path = workspace_root / "plugins" / "maibot-team_snowluma-adapter" / "config.toml"
            napcat_config_path = workspace_root / "plugins" / "maibot-team_napcat-adapter" / "config.toml"
            our_config_path = Path(__file__).resolve().parent / "config.toml"

            paths = []
            if snowluma_config_path.exists():
                paths.append(snowluma_config_path)
            if napcat_config_path.exists():
                paths.append(napcat_config_path)
            if our_config_path.exists():
                paths.append(our_config_path)

            for path in paths:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        content = f.read()
                    
                    doc = tomlkit.parse(content)
                    section_name = "plugin" if path == our_config_path else "chat"
                    
                    section = doc.get(section_name)
                    if section is None:
                        section = tomlkit.table()
                        doc[section_name] = section
                        
                    new_array = tomlkit.array()
                    for uid in merged_list:
                        new_array.append(uid)
                    section["ban_user_id"] = new_array
                    
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(doc.as_string())
                except Exception as e:
                    self.ctx.logger.error(f"启动同步配置文件 {path.name} 失败: {e}", exc_info=True)


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
            our_config_path = Path(__file__).resolve().parent / "config.toml"
            if our_config_path.exists():
                try:
                    with open(our_config_path, "r", encoding="utf-8") as f:
                        doc = tomlkit.parse(f.read())
                    plugin_sec = doc.get("plugin")
                    if plugin_sec is not None:
                        new_array = tomlkit.array()
                        for uid in new_list:
                            new_array.append(uid)
                        plugin_sec["ban_user_id"] = new_array
                        with open(our_config_path, "w", encoding="utf-8") as f:
                            f.write(doc.as_string())
                except Exception as e:
                    self.ctx.logger.error(f"清理自身黑名单配置失败: {e}", exc_info=True)

        self._sync_to_adapters(new_list)

    def _sync_to_adapters(self, new_list: List[str]) -> None:
        """将本插件配置的黑名单同步到 OneBot 适配器的配置。"""
        import tomlkit
        workspace_root = Path(os.getcwd())
        snowluma_config_path = workspace_root / "plugins" / "maibot-team_snowluma-adapter" / "config.toml"
        napcat_config_path = workspace_root / "plugins" / "maibot-team_napcat-adapter" / "config.toml"

        paths = []
        if snowluma_config_path.exists():
            paths.append(snowluma_config_path)
        if napcat_config_path.exists():
            paths.append(napcat_config_path)

        for path in paths:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                
                doc = tomlkit.parse(content)
                chat = doc.get("chat")
                if chat is None:
                    chat = tomlkit.table()
                    doc["chat"] = chat

                ban_list = chat.get("ban_user_id")
                if ban_list is None:
                    ban_list = tomlkit.array()
                    chat["ban_user_id"] = ban_list

                current_list = [str(x).strip() for x in ban_list if str(x).strip().isdigit()]
                norm_new_list = [str(x).strip() for x in new_list if str(x).strip().isdigit()]

                if set(current_list) != set(norm_new_list):
                    new_array = tomlkit.array()
                    for item in norm_new_list:
                        new_array.append(item)
                    chat["ban_user_id"] = new_array

                    with open(path, "w", encoding="utf-8") as f:
                        f.write(doc.as_string())
                    self.ctx.logger.info(f"成功将黑名单同步到适配器 {path.name}: {norm_new_list}")
            except Exception as e:
                self.ctx.logger.error(f"同步黑名单到适配器 {path} 失败: {e}", exc_info=True)

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

    async def _resolve_qq_from_msg_id(self, msg_id: str, stream_id: str = "") -> str:
        """通过上下文中的 msg_id 反查真实 QQ 号。"""
        msg_id = str(msg_id or "").strip()
        if not msg_id:
            return ""
        try:
            result = await self.ctx.message.get_by_id(
                message_id=msg_id,
                stream_id=stream_id or "",
            )
            return self._extract_user_id_from_message_payload(result)
        except Exception as e:
            self.ctx.logger.warning(f"通过 msg_id 反查 QQ 失败: msg_id={msg_id}, err={e}")
            return ""

    async def _resolve_qq_from_user_name(self, user_name: str, stream_id: str = "") -> str:
        """通过用户昵称/群名片在最近消息中反查 QQ 号。"""
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
        for item in reversed(messages):
            names = self._extract_user_names_from_message_payload(item)
            for name in names:
                if name == target_name or name.lower() == target_lower:
                    uid = self._extract_user_id_from_message_payload(item)
                    if uid.isdigit():
                        return uid
        return ""

    async def _resolve_target_qq(
        self,
        *,
        msg_id: str = "",
        user_name: str = "",
        user_id: str = "",
        stream_id: str = "",
    ) -> Tuple[str, str]:
        """解析最终要拉黑的 QQ 号。

        优先级：
        1. 已是纯数字 QQ
        2. msg_id 反查
        3. 用户名/群名片在最近消息中反查
        """
        raw_user_id = str(user_id or "").strip()
        if raw_user_id.isdigit():
            return raw_user_id, "user_id"

        resolved = await self._resolve_qq_from_msg_id(msg_id, stream_id=stream_id)
        if resolved:
            return resolved, "msg_id"

        resolved = await self._resolve_qq_from_user_name(user_name, stream_id=stream_id)
        if resolved:
            return resolved, "user_name"

        if raw_user_id and not raw_user_id.isdigit():
            resolved = await self._resolve_qq_from_msg_id(raw_user_id, stream_id=stream_id)
            if resolved:
                return resolved, "user_id_as_msg_id"
            resolved = await self._resolve_qq_from_user_name(raw_user_id, stream_id=stream_id)
            if resolved:
                return resolved, "user_id_as_name"

        return "", ""

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

    def _update_ban_list_everywhere(self, target_user_id: str, action: str) -> bool:
        """修改本插件以及选定适配器的 config.toml 中的 ban_user_id。"""
        target_uid_str = str(target_user_id).strip()
        if not target_uid_str or not target_uid_str.isdigit():
            return False

        import tomlkit
        workspace_root = Path(os.getcwd())
        adapter_type = self.config.plugin.adapter_type
        
        paths = []
        if adapter_type in ["snowluma", "all"]:
            p = workspace_root / "plugins" / "maibot-team_snowluma-adapter" / "config.toml"
            if p.exists(): paths.append(p)
        if adapter_type in ["napcat", "all"]:
            p = workspace_root / "plugins" / "maibot-team_napcat-adapter" / "config.toml"
            if p.exists(): paths.append(p)
        
        our_config_path = Path(__file__).resolve().parent / "config.toml"
        if our_config_path.exists():
            paths.append(our_config_path)

        if not paths:
            if adapter_type != "none":
                self.ctx.logger.warning(f"未找到选定适配器({adapter_type})或自身的 config.toml 配置文件")
            return False

        updated = False
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                
                doc = tomlkit.parse(content)
                section_name = "plugin" if path == our_config_path else "chat"
                
                section = doc.get(section_name)
                if section is None:
                    section = tomlkit.table()
                    doc[section_name] = section

                ban_list = section.get("ban_user_id")
                if ban_list is None:
                    ban_list = tomlkit.array()
                    section["ban_user_id"] = ban_list

                current_list = [str(x).strip() for x in ban_list if str(x).strip().isdigit()]

                file_updated = False
                if action == "add":
                    if target_uid_str not in current_list:
                        ban_list.append(target_uid_str)
                        file_updated = True
                        updated = True
                elif action == "remove":
                    idx = -1
                    for i, item in enumerate(ban_list):
                        if str(item).strip() == target_uid_str:
                            idx = i
                            break
                    if idx != -1:
                        ban_list.pop(idx)
                        file_updated = True
                        updated = True

                if file_updated:
                    # 清理列表中的非数字内容，一并写入
                    cleaned_array = tomlkit.array()
                    for item in ban_list:
                        val = str(item).strip()
                        if val.isdigit():
                            cleaned_array.append(val)
                    section["ban_user_id"] = cleaned_array

                    with open(path, "w", encoding="utf-8") as f:
                        f.write(doc.as_string())
                    self.ctx.logger.info(f"成功更新配置文件 {path.name}: {action} {target_uid_str}")
            except Exception as e:
                self.ctx.logger.error(f"更新配置文件 {path} 失败: {e}", exc_info=True)

        return updated

    def _get_current_ban_list(self) -> List[str]:
        """获取当前选定适配器中已屏蔽的 QQ 号合集。"""
        workspace_root = Path(os.getcwd())
        adapter_type = self.config.plugin.adapter_type
        paths = []
        if adapter_type in ["snowluma", "all"]:
            p = workspace_root / "plugins" / "maibot-team_snowluma-adapter" / "config.toml"
            if p.exists(): paths.append(p)
        if adapter_type in ["napcat", "all"]:
            p = workspace_root / "plugins" / "maibot-team_napcat-adapter" / "config.toml"
            if p.exists(): paths.append(p)

        if not paths:
            return []

        banned_set = set()
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    doc = tomlkit.parse(f.read())
                ban_list = doc.get("chat", {}).get("ban_user_id", [])
                for item in ban_list:
                    val = str(item).strip()
                    if val.isdigit():
                        banned_set.add(val)
            except Exception as e:
                self.ctx.logger.error(f"读取适配器黑名单列表失败 {path.name}: {e}")

        return sorted(list(banned_set))



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
            "当检测到用户发言包含无端谩骂、极其恶劣的人身攻击等行为时，调用此工具将该用户拉入全局屏蔽名单（拉黑）。"
            "上下文消息格式为 <message msg_id=\"数字\" user=\"昵称\" group_card=\"群名片\">，没有直接给出 QQ 号。"
            "请优先传违规消息的 msg_id；插件会自动反查真实 QQ 号并写入黑名单。不要传昵称当 QQ 号。"
        ),
        parameters=[
            ToolParameterInfo(
                name="msg_id",
                param_type=ToolParamType.STRING,
                description="违规消息的 msg_id。从上下文 <message msg_id=\"...\"> 中复制，插件会据此自动反查真实 QQ 号。",
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
                description="可选。违规用户的昵称/群名片（user 或 group_card）。仅作辅助匹配，不能代替 msg_id。",
                required=False,
            ),
            ToolParameterInfo(
                name="user_id",
                param_type=ToolParamType.STRING,
                description="可选。若已知纯数字 QQ 号可直接填写；未知时不要填昵称，请改填 msg_id。",
                required=False,
            ),
        ],
    )
    async def handle_ban_tool(
        self,
        msg_id: str = "",
        reason: str = "",
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

        target_qq, resolve_source = await self._resolve_target_qq(
            msg_id=msg_id,
            user_name=user_name,
            user_id=user_id,
            stream_id=stream_id,
        )
        if not target_qq:
            return {
                "success": False,
                "content": (
                    "拉黑失败：无法从上下文解析真实 QQ 号。"
                    "请从消息标签中传入 msg_id（例如 <message msg_id=\"1798286565\" user=\"枫\"> 中的 1798286565），"
                    "不要传入昵称/群名片。插件会自动反查 QQ 并加入黑名单。"
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
                    f"（解析来源：{resolve_source}，msg_id={str(msg_id or '').strip() or '无'}）。"
                ),
            }
        return {
            "success": False,
            "content": f"加入黑名单失败，可能是 QQ号 {target_qq} 已在名单中。",
        }


def create_plugin() -> BanManagerPlugin:
    """创建黑名单管理器插件实例。"""
    return BanManagerPlugin()

