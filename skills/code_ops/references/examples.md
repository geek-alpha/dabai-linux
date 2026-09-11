# code_ops 用法示例与常见问题

## 用法示例

### 检索
```
code_search(query="TODO", exts="py,js,ts", context=2)
code_search(query="def (fetch|save)\w+", root="/path/to/project")
code_search(query="timeout=120", regex=false, context=1)
```

### 定位与读取
```
code_locate(symbol="fetch_data")
code_locate(symbol="UserModel", kind="ref")
code_read(files="src/a.py:1-50, src/b.py")
```

### 分析与依赖
```
code_analyze(files="src/a.py, src/b.py")
code_analyze(root="/path/to/project")        # 整个目录统计
code_deps(root="/path/to/project")
```

### 修改（先读 → 再改 → 后验证 → 自审）
```
code_git_status(root="/path/to/project")
code_edit(file="src/a.py", mode="replace",
          old="timeout = 120", new="timeout = 300")
code_edit(file="src/a.py", mode="replace",
          old="timeout = 120", new="timeout = 300", preview=true)   # 先预览
code_edit(file="src/a.py", mode="insert",
          anchor="def main():", position="after",
          new="    setup_logger()")
code_verify(files="src/a.py")
code_test(root="/path/to/project")        # 跑全量测试
code_git_diff()                                # 看自己改了什么
code_review()                                  # 交付前自审
```

### git 感知与补丁
```
code_git_status()                     # 改前看工作区
code_git_log(file="src/a.py")         # 查这个文件最近的提交
code_git_blame(file="src/a.py", lines="10-20")   # 查某段代码是谁写的
code_patch(patch="diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1,3 +1,4 @@\n x=1\n+print(1)\n", preview=true)
```

## 常见问题
- 「锚点出现 N 次」→ 补上更多上下文（行首缩进、相邻行）让锚点唯一
- 「未找到要替换的原文」→ 先 code_read 核对原文（注意引号/缩进/换行）
- 中文文件乱码 → 本技能自动识别 UTF-8/GBK 编码，正常无需处理
- 文件没生效 → 确认写入了目标路径，并 code_verify 语法 + code_read 复核
- pytest 未安装 → code_test 会提示，改用 code_verify(mode=test) 逐文件跑，或先安装 pytest
- 补丁应用失败 → 多半是上下文行与文件不符，先 code_read 核对再生成补丁
