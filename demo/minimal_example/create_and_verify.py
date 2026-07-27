"""
最小文件创建与验证示例
演示：创建文件 → 验证内容 → 确认成功
"""
from pathlib import Path


def create_file(filepath: str, content: str) -> Path:
    """创建文件并写入内容"""
    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def verify_file(filepath: str, expected_content: str) -> bool:
    """验证文件内容是否与预期一致"""
    path = Path(filepath)
    if not path.exists():
        return False
    actual = path.read_text(encoding="utf-8")
    return actual == expected_content


def main():
    # 1. 创建
    filepath = "demo/minimal_example/output/hello.txt"
    content = "Hello, World!\n"
    path = create_file(filepath, content)
    print(f"[OK] 文件已创建: {path.absolute()}")

    # 2. 验证
    ok = verify_file(filepath, content)
    assert ok, "验证失败！"
    print("[OK] 验证通过: 内容一致")


if __name__ == "__main__":
    main()
