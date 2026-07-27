"""基础计算器 - 支持加减乘除四则运算"""


def add(a: float, b: float) -> float:
    """加法"""
    return a + b


def subtract(a: float, b: float) -> float:
    """减法"""
    return a - b


def multiply(a: float, b: float) -> float:
    """乘法"""
    return a * b


def divide(a: float, b: float) -> float:
    """除法"""
    if b == 0:
        raise ValueError("除数不能为零！")
    return a / b


def calculate(a: float, op: str, b: float) -> float:
    """根据运算符执行对应的运算"""
    operations = {
        "+": add,
        "-": subtract,
        "*": multiply,
        "x": multiply,
        "/": divide,
        "÷": divide,
    }

    if op not in operations:
        raise ValueError(f"不支持的运算符：'{op}'，请使用 +、-、*、/")

    return operations[op](a, b)


def main():
    """命令行交互式计算器"""
    print("=" * 40)
    print("      简易计算器（+ - * /）")
    print("      输入 'q' 退出程序")
    print("=" * 40)

    while True:
        try:
            expr = input("\n请输入算式（如 1 + 2）：").strip()
            if expr.lower() == "q":
                print("再见！")
                break

            if not expr:
                continue

            # 解析输入：按空格分割
            parts = expr.split()
            if len(parts) != 3:
                print("格式错误，请使用：数字 运算符 数字（如 3 + 5）")
                continue

            a, op, b = float(parts[0]), parts[1], float(parts[2])
            result = calculate(a, op, b)

            # 整数结果则不显示小数点
            if result == int(result):
                result = int(result)

            print(f"结果：{a} {op} {b} = {result}")

        except ValueError as e:
            print(f"错误：{e}")
        except KeyboardInterrupt:
            print("\n再见！")
            break
        except Exception as e:
            print(f"未知错误：{e}")


if __name__ == "__main__":
    main()
