"""Bounded upstream DuckDB/YARA implementations, no arbitrary SQL/flags."""
import json
import socket
import sys
from pathlib import Path

def _network_disabled(*args,**kwargs): raise PermissionError("Offline worker networking disabled")
socket.socket.connect=_network_disabled
socket.socket.connect_ex=_network_disabled
socket.socket.sendto=_network_disabled
socket.create_connection=_network_disabled
socket.getaddrinfo=_network_disabled

def main():
    data=json.loads(sys.stdin.read(4096))
    root=Path(data["root"]).resolve()
    def checked(filename,maximum):
        path=Path(filename).resolve()
        if root not in path.parents or not path.is_file() or path.stat().st_size>maximum: raise ValueError("Bounded workspace file required")
        if any(part.lower() in {"memory","sessions",".git",".tooling",".venv","node_modules"} for part in path.relative_to(root).parts): raise ValueError("Sensitive directory excluded")
        return path
    path=checked(data["path"],8_000_000)
    limit=max(1,min(100,int(data["limit"])))
    if data["tool"]=="yara":
        import yara
        rules=checked(data["rules_path"],256_000)
        compiled=yara.compile(source=rules.read_text(encoding="utf-8"),includes=False)
        matches=compiled.match(filepath=str(path),timeout=5)
        result={"success":True,"matches":[{"rule":item.rule,"tags":item.tags} for item in matches[:limit]],"total":len(matches),"coverage":"YARA_OFFLINE_RULE_MATCH"}
    elif data["tool"]=="duckdb":
        import duckdb
        config={"threads":1,"memory_limit":"64MB","autoload_known_extensions":False,"autoinstall_known_extensions":False}
        if path.suffix.lower()==".csv":
            with path.open("rb") as stream:
                if stream.read(2) in {b"\x1f\x8b",b"PK"}: raise ValueError("Compressed inputs excluded")
            with duckdb.connect(config=config) as connection:
                schema=connection.execute("DESCRIBE SELECT * FROM read_csv(?, max_line_size=262144, sample_size=1000)",[str(path)]).fetchall()
                count=connection.execute("SELECT count(*) FROM read_csv(?, max_line_size=262144, sample_size=1000)",[str(path)]).fetchone()[0]
                result={"success":True,"row_count":count,"columns":[{"name":row[0],"type":row[1]} for row in schema[:limit]],"coverage":"LOCAL_CSV_SCHEMA_AND_COUNT"}
        elif path.suffix.lower() in {".duckdb",".db"}:
            config["enable_external_access"]=False
            with duckdb.connect(str(path),read_only=True,config=config) as connection:
                result={"success":True,"tables":[row[0] for row in connection.execute("SHOW TABLES").fetchmany(limit)],"coverage":"DUCKDB_SCHEMA_ONLY"}
        else: raise ValueError("DuckDB adapter accepts CSV or DuckDB database files only")
    elif data["tool"]=="stix-taxii":
        from stix2 import parse
        if path.stat().st_size>2_000_000: raise ValueError("STIX JSON budget exceeded")
        bundle=parse(path.read_text(encoding="utf-8"),allow_custom=False)
        if bundle.type!="bundle": raise ValueError("A STIX bundle is required")
        result={"success":True,"records":[{key:str(row.get(key,"")) for key in ("id","type","name","created","modified")} for row in bundle.objects[:limit]],
                "total":len(bundle.objects),"coverage":"STIX2_VALIDATION_AND_METADATA; not TAXII transport"}
    else: raise ValueError("Unknown offline parser")
    print(json.dumps(result))

if __name__=="__main__":
    try: main()
    except Exception: print(json.dumps({"success":False,"error":"Offline parser rejected input or could not validate it"}));sys.exit(1)
