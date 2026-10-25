"""The knowledge library: CRAG + Self-RAG over Rectifi's guides, your notes, your entries and files you add.

    embeddings.py  Ollama embeddings with the nomic task prefixes
    loaders.py     GuideDirectoryLoader, NotesLoader, TransactionLoader, UploadLoader (+ optional PdfplumberLoader), splitting
    store.py       PgVectorStore: a LangChain VectorStore over the plain rag_chunks table (REAL[]), cosine + MMR in numpy
    index.py       incremental indexing (skips chunks whose content hash did not change)
    retrieve.py    MMR retrievers, HybridRetriever (vector + keyword, reciprocal-rank fusion), multi-query, strip compressor
    graders.py     prompted structured-output graders: DocGrade, GroundCheck, UseCheck, RouteDecision
    graph.py       build_rag_subgraph: one LangGraph, modes rag / crag / self_rag / full

Importing this package loads nothing heavy. Only the modules above load langchain. The API imports them lazily.
"""
