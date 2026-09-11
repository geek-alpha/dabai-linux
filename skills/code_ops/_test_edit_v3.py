# code_edit v3.0 升级自测文件
def renamed_func():
    # 注释里也有 old_func 字样，不应被 AST 命中
    return "old_func 在字符串里也不该被命中"


def target_func():
    x = 1
    y = 2
    return x
