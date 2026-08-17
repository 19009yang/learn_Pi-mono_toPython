# Phase 4.1 会话持久化实现解读

本文对应 `docs/learning-roadmap.md` 的 Phase 4.1 及其扩展点，解读 Python 版 Pi 的会话树、内存存储、append-only JSONL、本地 SQLite、上下文重建和仓库抽象。

## 1. 这一阶段解决什么问题

Phase 3 的 `Agent` 把消息保存在进程内存中。进程退出后，消息会消失；如果用户想回到较早的消息并继续提问，也没有数据结构保存被离开的旧分支。

Phase 4.1 增加的核心闭环是：

```text
AgentMessage
    ↓ append_message
Session
    ↓ append_entry
SessionStorage
    ├── InMemorySessionStorage
    ├── JsonlSessionStorage  → *.jsonl → open + replay
    └── SQLiteSessionStorage → sessions.db → SQL 查询
                                      ↓
                              恢复 entries 和 leaf_id
                                      ↓ build_context
                                 原始消息序列
```

这里有两个重要目标：

1. 同一套 `Session` API 可以使用内存或文件后端。
2. 切换分支只移动 leaf 指针，不删除或覆盖旧消息。

## 2. 文件结构

```text
pi_agent/session/
├── __init__.py   # Phase 4.1 公共 API
├── uuid.py       # 单调 UUIDv7
├── storage.py    # 数据类型、存储抽象、内存与 JSONL 后端、消息编解码
├── session.py    # 会话树的高层操作
├── repo.py       # 仓库抽象、内存与 JSONL 仓库
└── sqlite.py     # SQLite storage/repo、schema 与事务操作

test/test_session.py         # 内存与 JSONL 专项测试
test/test_sqlite_session.py  # SQLite 契约测试
```

模块之间的依赖方向是：

```text
uuid.py ← storage.py ← session.py ← repo.py
                 ↑
       pi_ai.types / pi_agent.messages
```

存储层不知道 Agent 如何调用模型；Agent 也不需要知道 JSONL 或 SQLite 的格式。SQLite 后端正是通过这个边界接入，没有修改 `Session` 的分支和上下文 API。

## 3. 会话树数据模型

### 3.1 SessionEntry

每条消息被包装为一个 `SessionEntry`：

```python
@dataclass(frozen=True)
class SessionEntry:
    id: str
    parent_id: str | None
    timestamp: int
    message: AgentMessage
```

- `id`：节点的唯一标识。
- `parent_id`：父节点 ID；根节点为 `None`。
- `timestamp`：写入会话的 Unix 毫秒时间。
- `message`：原始 `AgentMessage`，包括标准 LLM 消息和 Agent 自定义消息。

存储中还有一个 `leaf_id`，表示当前选中的叶节点。`parent_id` 链决定树结构，`leaf_id` 决定当前上下文使用哪条分支。

例如，顺序追加 A、B、C：

```text
A ← B ← C
        leaf = C
```

移动到 A 后追加 D：

```text
    ┌─ B ← C
A ←─┤
    └─ D
       leaf = D
```

B 和 C 仍保留在存储中。读取 C 的路径仍得到 `[A, B, C]`，读取当前 leaf 的路径得到 `[A, D]`。

### 3.2 为什么不把消息直接存成数组

普通数组适合线性聊天，但切回历史节点时通常只能截断尾部。截断会丢失原分支，也无法在两个分支间导航。

父指针树只需要为每个节点增加一个 `parent_id`，就能同时支持：

- 线性追加；
- 回到任意祖先；
- 从历史节点创建新分支；
- 保留并重新访问旧分支；
- 从任意叶节点重建上下文。

## 4. SessionStorage 抽象

`SessionStorage` 定义了会话树所需的最小持久化能力：

```python
await storage.get_metadata()
await storage.get_leaf_id()
await storage.set_leaf_id(entry_id)
await storage.append_entry(message)
await storage.get_entry(entry_id)
await storage.get_path_to_root(entry_id)
```

所有方法都是异步的。内存后端本身不需要文件 I/O，但统一使用异步接口后，调用方切换到文件、数据库或网络后端时不需要改变控制流。

### 4.1 InMemorySessionStorage

内存实现使用：

- `dict[str, SessionEntry]` 保存节点；
- `_leaf_id` 保存当前叶节点；
- `asyncio.Lock` 串行化追加和 leaf 移动；
- `deepcopy` 防止调用方通过保留的可变引用修改存储内部状态。

它适合单元测试、临时会话和作为其他后端的行为参考实现。

### 4.2 get_path_to_root

路径重建从 leaf 沿 `parent_id` 向根遍历，再反转结果：

```text
leaf → parent → parent → root
                  ↓ reverse
root → ... → parent → leaf
```

实现同时维护 `seen` 集合，以检测损坏数据中的父指针环；遇到不存在的节点也会立即报错，而不是返回一个不完整上下文。

## 5. append-only JSONL

### 5.1 文件记录

JSONL 文件的每一行都是一个完整 JSON 对象。第一行是版本化 header：

```json
{"type":"session","version":1,"id":"demo","created_at":1786492800000,"parent_session_id":null}
```

追加消息写入 `entry`：

```json
{"type":"entry","id":"...","parent_id":null,"timestamp":1786492800100,"message":{"kind":"user","content":"你好","timestamp":1786492800090}}
```

导航到其他节点写入 `leaf`：

```json
{"type":"leaf","leaf_id":"...","timestamp":1786492800200}
```

### 5.2 为什么 leaf 变化也要追加

如果只把 leaf 放在内存里，进程重启后无法知道用户最后查看的是哪个分支。如果原地修改 header，则文件不再是 append-only，也更容易在写入中断时破坏已有数据。

把 leaf 变化写成新记录后，“最后一条有效 leaf/entry 记录获胜”。旧记录保持不变，重启时按顺序重放即可恢复状态。

### 5.3 写入顺序

追加消息遵循：

```text
取得 asyncio.Lock
  → 读取当前 leaf 作为 parent_id
  → 验证消息可序列化
  → 追加并 fsync 一条 entry
  → 更新内存 entries 和 leaf
释放 Lock
```

内存状态只在文件成功写入后更新。因此文件追加失败时，调用者会收到异常，进程内状态不会假装写入成功。

`fsync` 让操作系统尽量把已确认的写入刷新到磁盘。它不能替代跨进程锁或事务型数据库，但对 MVP 的单写者 JSONL 模型提供了清晰的持久化边界。

### 5.4 重启恢复

`JsonlSessionStorage.open()` 会：

1. 验证 header 类型和版本。
2. 从第二行开始依次解析记录。
3. 验证 entry ID 不重复、父节点已存在。
4. 把每个 entry 加入内存索引并更新 leaf。
5. 重放 leaf 记录，恢复最后选择的分支。
6. 遇到未知记录、损坏 JSON 或悬空父节点时抛出 `SessionFormatError`。

这是一种事件重放模型：JSONL 是事实日志，内存字典和 leaf 是由日志派生出的当前状态。

## 6. AgentMessage 的显式编解码

当前实现支持以下消息：

- `UserMessage`
- `AssistantMessage`
- `ToolResultMessage`
- `BashExecutionMessage`
- `CustomMessage`
- `BranchSummaryMessage`
- `CompactionSummaryMessage`

同时支持这些内容块：

- `TextContent`
- `ImageContent`
- `ThinkingContent`
- `ToolCall`

编码使用显式 `kind` 判别字段，例如 `assistant`、`tool_result`、`thinking`。读取时只根据白名单构造已知 dataclass，不使用 `pickle`，也不会按文件内容动态导入或实例化任意类。

`Usage` 和 `CostInfo` 也会完整保存，所以恢复后的 assistant 消息仍保留 token 使用量和费用信息。

新增一种 `AgentMessage` 时，需要同步扩展：

1. `pi_agent/messages.py` 的 `AgentMessage` 联合类型及 `convert_to_llm()`（如果需要进入模型上下文）。
2. `storage.py` 的 `_encode_message()`。
3. `storage.py` 的 `_decode_message()`。
4. `test/test_session.py` 的序列化往返用例。

## 7. Session 高层 API

`Session` 把底层存储操作翻译成会话语义：

```python
session = Session(storage)

entry_id = await session.append_message(message)
branch = await session.get_branch()
messages = await session.build_context()
await session.move_to(older_entry_id)
```

### get_branch

返回 `SessionEntry` 列表，适合 UI 展示节点 ID、时间戳或调试树结构。

### build_context

返回根到目标 leaf 的 `AgentMessage` 列表，可以直接交给 Agent 状态或 `convert_to_llm()`。返回值经过复制，调用者修改列表或消息不会污染持久化状态。

### append_message

将消息接在当前 leaf 后面，并返回新 entry ID。

### move_to

移动 leaf 指针。传入 `None` 表示移动到空分支；随后追加的消息会成为新的根节点。

## 8. SessionRepo 抽象

Storage 管理一个已经存在的会话，Repo 管理多个会话的生命周期：

```python
repo = JsonlSessionRepo(".sessions")
session = await repo.create(session_id="demo")
same_session = await repo.open("demo")
metadata = await repo.list()
```

提供三种实现：

- `InMemorySessionRepo`：用字典保存多个内存 storage。
- `JsonlSessionRepo`：一个会话对应根目录下的一个 `<session_id>.jsonl` 文件。
- `SQLiteSessionRepo`：一个本地数据库保存多个会话及其全部树节点。

Repo 会验证 session ID，阻止 `../` 等路径逃逸形式；文件使用排他创建模式，已存在的会话不会被覆盖。

## 9. UUIDv7

UUIDv7 的高 48 位是 Unix 毫秒时间，因此字符串形式大致按生成时间排序，适合日志和数据库索引。

本实现的布局为：

```text
48-bit timestamp | version 7 | 12-bit sequence | RFC variant | 62-bit random
```

同一毫秒内使用递增的 12 位序列。序列溢出时推进逻辑毫秒，不阻塞线程，也不会生成倒序 ID。生成过程由线程锁保护。

## 10. 使用示例

### 10.1 内存会话

```python
from pi_ai.types import UserMessage
from pi_agent.session import InMemorySessionRepo

repo = InMemorySessionRepo()
session = await repo.create(session_id="scratch")

first = await session.append_message(UserMessage(content="第一问", timestamp=1))
second = await session.append_message(UserMessage(content="第二问", timestamp=2))

await session.move_to(first)
third = await session.append_message(UserMessage(content="从第一问创建分支", timestamp=3))

assert await session.get_leaf_id() == third
assert [message.content for message in await session.build_context()] == [
    "第一问",
    "从第一问创建分支",
]
```

### 10.2 JSONL 重启恢复

```python
from pi_agent.session import JsonlSessionRepo

repo = JsonlSessionRepo(".sessions")
session = await repo.create(session_id="durable-chat")
await session.append_message(...)

# 模拟另一个进程重新创建 Repo 并打开文件
restored_repo = JsonlSessionRepo(".sessions")
restored = await restored_repo.open("durable-chat")
messages = await restored.build_context()
```

### 10.3 SQLite 多会话恢复

```python
from pi_agent.session import SQLiteSessionRepo

repo = SQLiteSessionRepo(".pi/sessions.db")
session = await repo.create(session_id="durable-chat")
await session.append_message(...)

# 新 Repo 实例模拟进程重启；同一数据库也可保存其他 session ID。
restored = await SQLiteSessionRepo(".pi/sessions.db").open("durable-chat")
messages = await restored.build_context()
```

SQLite schema 使用 `sessions` 保存元数据和当前 leaf，使用 `session_entries` 保存消息树节点。追加消息与更新 leaf 位于同一 `BEGIN IMMEDIATE` 事务；连接启用 foreign keys、busy timeout 和 WAL。`PRAGMA user_version` 记录 schema 版本，不兼容版本会明确报错。

## 11. 测试覆盖

`test/test_session.py` 覆盖：

- UUIDv7 的版本位、variant、唯一性和单调顺序；
- 内存会话追加、移动 leaf、创建分支和读取旧分支；
- JSONL 重启后恢复当前 leaf；
- 从非当前 leaf 恢复完整旧分支；
- 七类消息和四类内容块的序列化往返；
- Repo 创建、打开、列出、重复 ID、非法 ID和缺失会话；
- 非法 leaf、重复 entry ID、非 JSON details；
- 损坏 JSON 和悬空 parent。

`test/test_sqlite_session.py` 额外覆盖全部消息类型重启恢复、分支、多会话共库、重复记录、损坏消息 JSON 和 schema 版本错误。

运行专项测试：

```bash
uv run pytest test/test_session.py -v
```

运行完整回归：

```bash
uv run pytest -v
```

## 12. 当前边界与后续扩展

当前仍未实现：

- SQLite schema 自动迁移；
- 删除、合并或压缩 JSONL 日志；
- 远程会话存储；
- 分支摘要自动生成。

CLI 已通过 `AgentHarness` 自动绑定会话生命周期：`--session <id>` 默认使用 `<cwd>/.pi/sessions.db`，可用 `--session-db` 改路径；原 JSONL 模式通过 `--session-backend jsonl --sessions-dir <dir>` 保留。不传 `--session` 时仍使用内存后端。
