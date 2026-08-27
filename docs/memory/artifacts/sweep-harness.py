import os,glob,re,subprocess,json,sys,shutil
from pathlib import Path
root=Path("/tmp/claude-0/-home-user-c2hlsc-agent/b456695b-a3c9-52de-a91c-d45a09f68659/scratchpad/hlseval/hls_eval_data")
work=Path("/tmp/claude-0/-home-user-c2hlsc-agent/b456695b-a3c9-52de-a91c-d45a09f68659/scratchpad/sweep_A"); shutil.rmtree(work,ignore_errors=True); work.mkdir(parents=True)
rows=[]
for fam in sorted(os.listdir(root)):
    fp=root/fam
    if not fp.is_dir(): continue
    for d in sorted(os.listdir(fp)):
        dd=fp/d
        if not (dd/'top.txt').exists(): continue
        cpps=[f for f in glob.glob(str(dd/'*.cpp')) if not f.endswith('_tb.cpp')]
        hs=glob.glob(str(dd/'*.h'))
        if not cpps: continue
        top=(dd/'top.txt').read_text().strip()
        raw=(open(hs[0],errors='ignore').read()+"\n" if hs else "")+open(cpps[0],errors='ignore').read()
        raw=re.sub(r'^\s*#include\s+"[^"]*"','',raw,flags=re.M)
        sysinc=sorted(set(re.findall(r'^\s*#include\s+<[^>]*>',raw,flags=re.M)))
        raw=re.sub(r'^\s*#include\s+<[^>]*>','',raw,flags=re.M)
        raw=re.sub(r'^\s*#pragma\s+once','',raw,flags=re.M)
        pd=work/f"{fam}__{d}"; pd.mkdir(parents=True)
        (pd/'raw.c').write_text(raw)
        # macro-expand so array bounds/loop bounds become literals
        pp=subprocess.run(['gcc','-E','-P','-x','c',str(pd/'raw.c')],capture_output=True,text=True)
        if pp.returncode!=0:
            rows.append(dict(fam=fam,d=d,top=top,stage='preprocess',verdict='FAIL',why=pp.stderr.strip().splitlines()[-1][:90] if pp.stderr.strip() else 'cpp error')); continue
        (pd/'input.c').write_text("\n".join(sysinc)+"\n"+pp.stdout)
        src=(pd/'input.c').read_text()
        m=re.search(r'[A-Za-z_][\w \*]*?\b'+re.escape(top)+r'\s*\(([^;{]*)\)\s*\{',src,re.S)
        argblk=""
        if m:
            parts,depth,cur=[],0,''
            for ch in m.group(1):
                if ch==',' and depth==0: parts.append(cur); cur=''; continue
                cur+=ch
                if ch in '([<': depth+=1
                elif ch in ')]>': depth-=1
            if cur.strip(): parts.append(cur)
            lines=[]
            for p in parts:
                p=p.strip()
                if not p or p=='void': continue
                dims=re.findall(r'\[([^\]]*)\]',p)
                core=re.sub(r'\[[^\]]*\]','',p)
                nm=re.sub(r'\*',' ',core).split()[-1]
                if dims:
                    try: L=int(eval(dims[0],{},{})) if dims[0].strip() else None
                    except Exception: L=None
                    if L: lines.append(f"  {nm}:\n    length: {L}")
                elif '*' in core:
                    lines.append(f"  {nm}:\n    length: 1")
            if lines: argblk="arguments:\n"+"\n".join(lines)+"\n"
        (pd/'config.yaml').write_text(f"input_files:\n  - input.c\ntop: {top}\nnum_tests: 4\nseed: 2\n"+argblk)
        r=subprocess.run([sys.executable,'-m','c2hlsc_agent.cli','convert','--config',str(pd/'config.yaml'),
                          '--out',str(pd/'proj'),'--no-llm','--no-run-vitis','--new-run'],
                         capture_output=True,text=True,cwd='/home/user/c2hlsc-agent',timeout=180)
        rep=pd/'proj'/'conversion_report.md'
        log=pd/'proj'/'software_equivalence.log'
        if not rep.exists():
            rows.append(dict(fam=fam,d=d,top=top,stage='convert-crash',verdict='FAIL',why=(r.stderr.strip().splitlines() or [''])[-1][:90])); continue
        txt=rep.read_text()
        if '**PASS**' in txt:
            rows.append(dict(fam=fam,d=d,top=top,stage='host-equiv',verdict='PASS',why='')); continue
        if 'static diagnostics contain errors' in txt:
            codes=re.findall(r'\|\s*(dynamic-allocation|unsupported-stdlib-call|system-call|file-io|function-pointer|unbounded-loop|recursion|pointer-arithmetic|variable-length-array)\s*\|',txt)
            rows.append(dict(fam=fam,d=d,top=top,stage='static-analysis',verdict='FAIL',why=','.join(sorted(set(codes))) or 'diagnostics')); continue
        why='unknown'
        if log.exists():
            L=log.read_text()
            m=re.findall(r'error: ([^\n]{0,80})',L)
            if m: why=m[0]
            elif 'Mismatch' in L: why='golden-vs-hls mismatch'
        rows.append(dict(fam=fam,d=d,top=top,stage='host-compile/equiv',verdict='FAIL',why=why))
json.dump(rows,open(work/'rows.json','w'),indent=1)
from collections import Counter
print("TOTAL",len(rows),"PASS",sum(1 for r in rows if r['verdict']=='PASS'))
c=Counter((r['fam'],r['verdict']) for r in rows)
print(f"{'family':11} {'n':>3} {'PASS':>4} {'FAIL':>4}")
for f in sorted(set(r['fam'] for r in rows)):
    n=sum(1 for r in rows if r['fam']==f); print(f"{f:11} {n:3d} {c[(f,'PASS')]:4d} {c[(f,'FAIL')]:4d}")
print("\nFAIL stage histogram:"); 
for k,v in Counter(r['stage'] for r in rows if r['verdict']=='FAIL').most_common(): print("  ",k,v)
print("\nFAIL why histogram:")
for k,v in Counter(r['why'][:45] for r in rows if r['verdict']=='FAIL').most_common(15): print("  ",k,"->",v)
