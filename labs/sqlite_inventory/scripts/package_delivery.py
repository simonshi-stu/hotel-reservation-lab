"""Prepare only synthetic data, portable source and evidence for native file attachments."""
import csv
import hashlib
import html
import io
import json
import re
import shutil
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
DEST=ROOT/"delivery"/"20261104-v2"
TABLES=("hotels","room_types","customers","nightly_inventory","reservations","reservation_nights","inventory_events")
def sha(data):
    return hashlib.sha256(data).hexdigest()
def markdown(text):
    out=[]
    in_code=False
    table=False
    for line in text.splitlines():
        if line.startswith("~~~"):
            if table: out.append("</tbody></table></div>"); table=False
            out.append("</code></pre>" if in_code else "<pre><code>")
            in_code=not in_code
        elif in_code:
            out.append(html.escape(line)+"\n")
        elif line.startswith("|"):
            if re.fullmatch(r"[|\s:\-]+",line): continue
            if not table:
                out.append('<div class="scroll"><table><tbody>')
                table=True
            cells=line.strip("|").split("|")
            out.append("<tr>"+"".join("<td>"+html.escape(c.strip())+"</td>" for c in cells)+"</tr>")
        else:
            if table: out.append("</tbody></table></div>"); table=False
            if line.startswith("#"):
                level=min(4,len(line)-len(line.lstrip("#")))
                out.append("<h"+str(level)+">"+html.escape(line.lstrip("#").strip())+"</h"+str(level)+">")
            elif line.strip():
                clean=re.sub(r"\[([^\]]+)\]\(([^)]+)\)",r"\1 [\2]",line)
                out.append("<p>"+html.escape(clean).replace("**","")+"</p>")
    if table: out.append("</tbody></table></div>")
    if in_code: out.append("</code></pre>")
    return "\n".join(out)

def main():
    if DEST.exists(): raise FileExistsError("Refusing overwrite: "+str(DEST))
    DEST.mkdir(parents=True)
    data=ROOT/"data"/"synthetic-v1"
    manifest=json.loads((data/"manifest.json").read_text(encoding="utf-8"))
    rows={}
    findings=[]
    for name in TABLES:
        raw=(data/(name+".csv")).read_bytes()
        assert sha(raw)==manifest["csv_sha256"][name+".csv"]
        rows[name]=list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        assert len(rows[name])==manifest["row_counts"][name]
        shutil.copyfile(data/(name+".csv"),DEST/(name+".csv"))
    for row in rows["customers"]:
        if not row["name"].startswith("SIMULATED Guest "): findings.append("customer name")
        if not re.fullmatch(r"sim-guest-\d+@example\.invalid",row["email"]): findings.append("customer email")
    for name in ("hotels","room_types"):
        if any(not r["name"].startswith("SIMULATED ") for r in rows[name]): findings.append(name+" names")
    if set(rows["customers"][0])!={"id","name","city","segment","email"}: findings.append("unexpected customer fields")
    assert not findings,findings
    package={}
    for path in [ROOT/"README.md",ROOT/"AGENTS.md",ROOT/".gitignore",
                 *sorted((ROOT/"hotel").glob("*.py")),*sorted((ROOT/"hotel").glob("*.sql")),
                 *sorted((ROOT/"docs").glob("*.md")),*sorted((ROOT/"tests").glob("*.py")),
                 *[ROOT/"scripts"/n for n in ("__init__.py","seed.py","support.py","verify.py","load.py","reproduce.py","review_503.py","run.ps1")],
                 *sorted((ROOT/"reports").glob("*.json")),*sorted((ROOT/"reports").glob("*.txt")),
                 *sorted(data.glob("*"))]:
        if not path.is_file(): continue
        relative=path.relative_to(ROOT).as_posix()
        raw=path.read_bytes()
        if path.name=="README.md":
            raw=raw.decode("utf-8-sig").replace("数据库已生成，无需重新 seed。","附件不含运行库；先运行 scripts.seed 模块生成数据库。").encode("utf-8")
        if path.name=="run.ps1":
            text=raw.decode("utf-8-sig")
            text=re.sub(r"'C:\\Users\\[^']+?python\.exe'", "(Get-Command python -ErrorAction Stop).Source",text)
            assert "C:\\Users\\" not in text
            raw=text.encode("utf-8")
        package[relative]=raw
    sensitive_patterns={
        "private_key":r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        "aws_access_key":r"\bAKIA[A-Z0-9]{16}\b",
        "github_token":r"\bgh[pousr]_[A-Za-z0-9]{30,}\b",
        "openai_token":r"\bsk-[A-Za-z0-9_-]{24,}\b",
        "machine_user_path":r"C:\\Users\\[A-Za-z0-9_.-]+\\",
        "credential_url":r"[a-z]+://[^\s/:]+:[^\s/@]+@",
    }
    hits=[]
    for relative,raw in package.items():
        text=raw.decode("utf-8-sig")
        for label,pattern in sensitive_patterns.items():
            if re.search(pattern,text): hits.append({"file":relative,"type":label})
    assert not hits,hits
    sections=[
        '<section class="notice"><h2>全部为合成数据</h2><p>3 家酒店、12 类房型、2,000 位客户、60 天日历、1,192 笔订单。名称均带 SIMULATED，邮箱为 example.invalid；没有真实住客或支付数据。</p><p>这是 SQLite 本机实验，未验证 PostgreSQL。完整七张 CSV 可从本次对话附件下载，ZIP 含全部数据、字段字典、生成脚本、复现步骤和测试证据。</p></section>',
        '<section><h2>先读 503 结论</h2><p>2,450 次请求中，1,216 次为入业务前的 server_busy；没有 database_busy。新键预订有 1,440 次，81 次建单、297 次无房、1,062 次过载拒绝。不能将拒绝响应计为完成订单，也不能据此给出吞吐容量。</p><p>24 项功能检查通过。独立复核确认拒绝分支没有数据库副作用，同时复现了应用槽不限制连接线程的资源保护缺口。</p></section>',
        "<section><h2>七张 CSV 的样本（各前 8 行）</h2>"]
    for name in TABLES:
        sample=rows[name][:8]
        sections.append("<details open><summary>"+name+".csv — "+str(len(rows[name]))+" 行</summary><div class='scroll'><table><thead><tr>"+
            "".join("<th>"+html.escape(c)+"</th>" for c in sample[0])+"</tr></thead><tbody>"+
            "".join("<tr>"+"".join("<td>"+html.escape(v)+"</td>" for v in r.values())+"</tr>" for r in sample)+
            "</tbody></table></div></details>")
    sections.append("</section>")
    for filename in ("01-data.md","02-design.md","03-verification.md","04-503-review.md"):
        sections.append("<section>"+markdown((ROOT/"docs"/filename).read_text(encoding="utf-8"))+"</section>")
    page='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>酒店合成数据与503复核</title><style>body{margin:0;background:#f1f5f9;color:#172438;font:16px/1.75 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1160px;margin:auto;padding:32px 20px 64px}header,section{background:white;padding:26px;margin:20px 0;border-radius:14px;border:1px solid #d9e2ef}h1{font-size:32px;line-height:1.3}h2{font-size:23px}h3{font-size:19px;color:#244f79}.tag{display:inline-block;color:#9b3515;background:#ffedd5;padding:3px 12px;border-radius:20px;font-weight:700}.notice{border-left:5px solid #d97706}.scroll{overflow:auto;margin:12px 0}table{border-collapse:collapse;font-size:13px;white-space:nowrap;width:100%}td,th{padding:8px 11px;border:1px solid #d7e1ee;text-align:left}th,tbody tr:first-child{background:#f0f5fb}pre{background:#10233c;color:#e4eefb;padding:18px;overflow:auto;border-radius:8px;white-space:pre-wrap;font-size:13px}details{padding:12px 0}summary{font-weight:700;cursor:pointer}p{overflow-wrap:anywhere}</style><main><header><span class="tag">模拟数据 / SYNTHETIC ONLY</span><h1>酒店预订：数据、实验与503复核</h1><p>可直接阅读的七表样本、完整字段字典、复现说明与证据边界。生成日期：2026-11-04。页面无需联网，不包含外部脚本或资源。</p></header>'+''.join(sections)+"</main></html>"
    preview=DEST/"hotel-synthetic-data-and-503-review.html"
    preview.write_text(page,encoding="utf-8")
    package["hotel-synthetic-data-and-503-review.html"]=preview.read_bytes()
    guide="""# 附件使用说明
所有数据均为合成数据；CSV 使用 UTF-8 BOM。直接打开 HTML 可看七表样本、字段字典、测试说明及503独立复核；所有内容已内嵌，无需网络。
data/synthetic-v1 内包含七张完整 CSV 与种子、规模、文件哈希清单。
docs/01-data.md 是完整字段、关联和构造分布；docs/04-503-review.md 是独立503复核。
原始功能、负载和复现结果及新503探针在 reports 中。保留原报告 passed 字段；它仅表示既有断言通过，并不表示所有业务完成或可用性目标达标。
scripts/seed.py 为生成脚本；scripts/reproduce.py 重建数据并核对哈希；scripts/verify.py 跑功能测试；scripts/load.py 重跑本机HTTP实验；scripts/review_503.py 重跑503复核。
代码仅使用 Python 3.10+ 标准库，不安装依赖、不访问真实酒店或支付系统。PostgreSQL 尚未验证。
若解压，请使用 E:\Projects 下的新目录，避免覆盖现有项目。新解压目录没有运行数据库；先在该目录通过以下命令生成：
.\\scripts\\run.ps1 scripts.seed
.\\scripts\\run.ps1 scripts.verify
.\\scripts\\run.ps1 scripts.load
.\\scripts\\run.ps1 scripts.reproduce
.\\scripts\\run.ps1 scripts.review_503
推荐用 scripts/run.ps1 包装运行以上模块，它会将临时目录和缓存设在当前项目.runtime。附件中的启动脚本已改为从当前 PATH 查找 python；如 PATH 是不可用的Windows商店别名，请将当前进程 HOTEL_PYTHON 指向已安装的基础解释器。不应把本机用户目录写入共享附件。
无现成运行库时，启动前先 scripts.seed。服务仅监听127.0.0.1，无公网身份认证。
本包不包含 .runtime、数据库、缓存、环境变量或其他项目文件。
"""
    package["DELIVERY_README.md"]=guide.encode("utf-8")
    privacy={"synthetic":True,"customer_rows_checked":len(rows["customers"]),
             "customer_names_synthetic":True,"customer_email_reserved_invalid_domain":True,
             "hotel_and_room_names_synthetic":True,"unexpected_customer_fields":False,
             "sensitive_pattern_findings":hits,"scope":"Seven complete CSVs and every UTF-8 file in this allowlisted bundle.",
             "exclusions":[".runtime","databases","caches","environment variables","unrelated projects","library helpers"],
             "portable_launcher":"Removed machine-specific installed Python path from bundled launcher only.",
             "limits":"Provenance and schema checks plus pattern scan; not a universal proof against every possible secret.",
             "csv_sha256":manifest["csv_sha256"]}
    package["PRIVACY_CHECK.json"]=json.dumps(privacy,ensure_ascii=False,indent=2).encode("utf-8")
    package["DELIVERY_MANIFEST.json"]=json.dumps(
        {"files":{name:{"bytes":len(raw),"sha256":sha(raw)} for name,raw in sorted(package.items())}},
        ensure_ascii=False,indent=2).encode("utf-8")
    for relative,raw in package.items():
        for label,pattern in sensitive_patterns.items():
            if re.search(pattern,raw.decode("utf-8-sig")): raise AssertionError((relative,label))
    target=DEST/"hotel-reservation-synthetic-data-and-tests.zip"
    with zipfile.ZipFile(target,"w",zipfile.ZIP_DEFLATED) as archive:
        for name,raw in sorted(package.items()):
            archive.writestr("hotel-reservation-lab/"+name,raw)
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert len(archive.namelist())==len(package)
        for name,raw in package.items():
            assert archive.read("hotel-reservation-lab/"+name)==raw
    (DEST/"privacy-check.json").write_text(json.dumps(privacy,ensure_ascii=False,indent=2),encoding="utf-8")
    paths=[str(preview),str(target)]+[str(DEST/(name+".csv")) for name in TABLES]
    request_doc={"uploads":[{"local_path":path,"purpose":"create_library_file",
                 "library_artifact_type":"report" if path.endswith(".html") else "other"} for path in paths]}
    (ROOT/".runtime"/"library-delivery-20261104"/"upload-request.json").write_text(
        json.dumps(request_doc,ensure_ascii=False),encoding="utf-8")
    receipt={"attachments":[{"name":Path(p).name,"bytes":Path(p).stat().st_size,
              "sha256":sha(Path(p).read_bytes())} for p in paths],
             "archive_file_count":len(package),"privacy_check_passed":not findings and not hits}
    (DEST/"delivery-receipt.json").write_text(json.dumps(receipt,indent=2),encoding="utf-8")
    print(json.dumps(receipt,indent=2))

if __name__=="__main__":
    main()

