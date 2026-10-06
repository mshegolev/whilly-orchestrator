## ADDED Requirements

### Requirement: Canonical email identity for the admin permission guard

The admin-users permission guard MUST resolve the authenticated user through
the canonical session-email lookup, including the seeded real email
`admin@whilly.local`. It MUST apply the existing admin-role permission check
unchanged and MUST fail closed when the session email is missing or not unique.
This requirement MUST NOT grant rights by bypassing the permission guard.

#### Scenario: Seeded admin reaches the existing admin guard

- **WHEN** the authenticated session email is `admin@whilly.local`
- **THEN** the guard resolves that user through the canonical email lookup and
  applies the existing admin permission

#### Scenario: Non-admin remains forbidden

- **WHEN** a valid session resolves to an operator without the admin role
- **THEN** the route returns `403` through the same permission guard

#### Scenario: Ambiguous identity fails closed

- **WHEN** the canonical session-email lookup cannot establish one unique user
- **THEN** the request is rejected and no admin rights are granted
