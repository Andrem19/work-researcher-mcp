import io

h = io.open("chunk_0.txt", encoding="utf-8", errors="replace").read()
print("len:", len(h))
print("head 150:", repr(h[:150]))
print("tail 50:", repr(h[-50:]))
