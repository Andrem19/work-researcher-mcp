import io
import subprocess

# remove from git
subprocess.run(["git", "rm", "-q"] +
               [f"chunk_{i}.txt" for i in range(13)], check=True)

# update .gitignore
h = io.open(".gitignore", encoding="utf-8").read()
if "chunk_" not in h:
    h += ("\n# leftover base64 chunks from failed browser-injection attempts\n"
          "chunk_*.txt\ncover_b64.txt\n")
    io.open(".gitignore", "w", encoding="utf-8").write(h)
    print("gitignore updated")
