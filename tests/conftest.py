"""测试环境准备：必须在导入 app 之前把数据库指向临时文件。"""

import os
import pathlib
import tempfile

_DB = pathlib.Path(tempfile.gettempdir()) / "sedentary_assistant_test.db"

os.environ["DATABASE_URL"] = f"sqlite:///{_DB.as_posix()}"
os.environ["LLM_BASE_URL"] = ""
os.environ["LLM_API_KEY"] = ""
