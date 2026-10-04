-- 203_b_vector_hnsw.sql — Workstream B. Bất biến sau khi áp dụng.
-- Index HNSW (cosine) cho memory_items.embedding. Cột `embedding` khai báo `vector` không cố định số chiều (đổi embedding model
-- không cần migration) nên index là INDEX BIỂU THỨC MỘT PHẦN theo số chiều: (embedding::vector(256)) WHERE vector_dims(embedding)=256.
-- 256 = config/brain.yaml embedding.dim mặc định. Số chiều khác: PgMemoryStore.ensure_hnsw_index(dim) (CREATE INDEX CONCURRENTLY).
-- Truy vấn của PgBrainRetriever dùng đúng biểu thức này khi thấy index (memory_items_emb_hnsw_<dim>). Thiếu pgvector hoặc
-- pgvector < 0.5 (không có HNSW) => bỏ qua, truy vấn vẫn chạy bằng quét tuần tự chính xác.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')
       AND EXISTS (SELECT 1 FROM pg_am WHERE amname = 'hnsw')
       AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'memory_items' AND column_name = 'embedding') THEN
        CREATE INDEX IF NOT EXISTS memory_items_emb_hnsw_256 ON memory_items
            USING hnsw ((embedding::vector(256)) vector_cosine_ops) WITH (m = 16, ef_construction = 64)
            WHERE vector_dims(embedding) = 256;
    ELSE
        RAISE NOTICE 'pgvector/HNSW không khả dụng: bỏ qua index vector (truy vấn dùng quét tuần tự)';
    END IF;
END
$$;
