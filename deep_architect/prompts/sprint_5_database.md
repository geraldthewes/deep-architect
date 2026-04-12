# Sprint 5: Database + Knowledge Base Guidance
<!-- Bootstrap: PRD §5.2 Sprint 5 -->

## Goal
Produce `database/c2-container.md` and any supporting documents
(e.g. `database/schema.md`, `database/vector-store.md`, `database/caching.md`).

## What to Document
- Primary relational/document database choice and rationale
- Vector store / knowledge base (if AI features are in scope)
- Caching strategy and technology
- Data partitioning and scaling approach
- Backup and recovery strategy
- Migration strategy

## C4Container for Database Layer
Show containers within the data boundary:
- Primary database container
- Vector/embedding store container (if applicable)
- Cache container (Redis, Memcached, etc.)
- Object storage (S3-compatible, if applicable)
- Search index (Elasticsearch/OpenSearch, if applicable)

## File Structure for database/c2-container.md
1. `# Database + Knowledge Base Container` heading
2. Narrative describing data architecture
3. Mermaid C4Container diagram scoped to data layer
4. `## Data Models` section (high-level entity descriptions)
5. `## Knowledge Base Architecture` section (if AI/RAG is in scope)
6. `## Scaling and Performance` section

## Additional Files (create if PRD warrants)
- `database/schema.md`: key entities, relationships, indexing strategy
- `database/vector-store.md`: embedding model, chunking strategy, retrieval approach
- `database/caching.md`: cache strategy, TTL policies, invalidation

## Quality Checklist
- [ ] All data stores from PRD are represented
- [ ] Vector store configuration documented if RAG/AI search is in scope
- [ ] Cache layer included with justification
- [ ] Data retention and backup strategy mentioned
- [ ] Migration tooling specified (Alembic, Flyway, etc.)
