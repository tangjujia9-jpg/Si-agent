"""Product capability metadata shared by validation, policy and the console."""

TOOLS = {
    "search_memory": ("检索记忆", "read", "analysis"),
    "read_memory": ("读取记忆", "read", "analysis"),
    "propose_memory": ("提出记忆建议", "memory", "analysis"),
    "change_memory": ("修改记忆", "memory", "analysis"),
    "forget_memory": ("忘记记忆", "memory", "analysis"),
    "list_project_files": ("浏览项目文件", "read", "analysis"),
    "read_project_file": ("读取项目文件", "read", "analysis"),
    "search_project_files": ("搜索项目文件", "read", "analysis"),
    "read_operation_result": ("回读工具结果", "read", "analysis"),
    "git_status": ("检查工作区状态", "read", "analysis"),
    "git_diff": ("查看代码差异", "read", "analysis"),
    "git_log": ("查看提交历史", "read", "analysis"),
    "update_plan": ("更新任务计划", "plan", "development"),
    "apply_project_patch": ("修改项目文件", "write_files", "development"),
    "run_project_command": ("执行项目命令", "execute_commands", "development"),
    "get_command_result": ("查看命令结果", "read", "development"),
    "stop_project_command": ("停止项目命令", "cancel", "development"),
}


def allowed_tools(requested, mode, permissions):
    return [name for name in dict.fromkeys(requested) if name in TOOLS
            and (TOOLS[name][2] == "analysis" or mode == "development")
            and (TOOLS[name][1] not in {"write_files", "execute_commands"}
                 or permissions.get(TOOLS[name][1], False))]
