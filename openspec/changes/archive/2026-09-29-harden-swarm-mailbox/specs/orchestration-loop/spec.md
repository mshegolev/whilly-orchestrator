## MODIFIED Requirements

### Requirement: Task-local mailbox IPC
The task-local mailbox contract MUST be available to offline workers.
When `WHILLY_SWARM_MAILBOX` is set, worker `message` and `inbox` operations
MUST use task-local JSON IPC without PostgreSQL credentials or network access.
The coordinator MUST drain requests, pin the sender to the task identity,
validate session and recipient through the durable store, refresh the scoped
inbox, and write `delivered`, `rejected`, or acknowledgement receipts. The
mailbox transport MUST use one descriptor-anchored boundary for enqueue,
request draining, receipts, inbox state, proposal status, and removals.

#### Scenario: Worker sends while offline
- **WHEN** a worker writes a valid message request to its task-local outbox
- **THEN** the coordinator later validates and persists it, and the worker can
  observe a delivery receipt and refreshed scoped inbox

#### Scenario: Crash after durable delivery
- **WHEN** the coordinator commits a message and crashes before completing
  receipt handling
- **THEN** the request may be retried or redelivered; delivery is at-least-once,
  not exactly-once

#### Scenario: Special files cannot block or dispatch
- **WHEN** an outbox entry is a FIFO, symlink, malformed JSON document, or oversized request
- **THEN** the coordinator SHALL use no-follow nonblocking bounded reads and SHALL NOT invoke the service for that entry
- **AND** it SHALL write a named rejection receipt through the opened receipts descriptor when safe, while retaining the original special file for inspection and avoiding payload retry

#### Scenario: Durable delivery receipt
- **WHEN** a valid request is persisted by the service
- **THEN** the coordinator SHALL write a durable receipt through a descriptor-relative temporary file and atomic replacement
- **AND** the request SHALL be removed only after receipt publication succeeds

#### Scenario: Mailbox directory is replaced while service is awaited
- **WHEN** an outbox or receipts directory is renamed and recreated during an awaited service call
- **THEN** the coordinator SHALL continue using the descriptors opened for that attempt
- **AND** it SHALL NOT modify files in the replacement directories

#### Scenario: Mailbox path ancestors are untrusted
- **WHEN** a mailbox ancestor is a symlink or is swapped to a symlink before the mailbox is opened
- **THEN** the coordinator SHALL open or create path components one at a time with directory descriptors and no-follow checks
- **AND** it SHALL reject the mailbox without modifying the symlink target

#### Scenario: Inbox and status state are bounded
- **WHEN** inbox or proposal status state is read
- **THEN** the adapter SHALL read only regular files through the opened mailbox root descriptor
- **AND** a state document exceeding 4 MiB SHALL raise an explicit error rather than being truncated

#### Scenario: Directory scan and backlog are bounded
- **WHEN** the coordinator scans an outbox containing valid and invalid names
- **THEN** it SHALL inspect at most 100 directory entries per tick, count all inspected entries for the enqueue backlog cap, and avoid materializing or sorting the entire directory

#### Scenario: Atomic write failure cleans temporary state
- **WHEN** an exclusive mailbox temporary file fails during write or fsync
- **THEN** the coordinator SHALL close and remove that temporary file before propagating the error
