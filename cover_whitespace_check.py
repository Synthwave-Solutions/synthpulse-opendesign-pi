#!/usr/bin/env python3
"""Thorough cover check: flags top whitespace AND a missing/hidden cover logo.
Usage: cover_whitespace_check.py page1.png [label]"""
import sys
from PIL import Image
def check(png,label=''):
    im=Image.open(png).convert('RGB'); w,h=im.size; px=im.load()
    white=lambda y: sum(1 for x in range(0,w,3) if min(px[x,y])>245)/(w//3)
    dark =lambda y: sum(1 for x in range(0,w,3) if px[x,y][0]<60 and px[x,y][1]<70 and px[x,y][2]<95)/(w//3)
    topwhite=next((y for y in range(h) if white(y)<0.92),h)/h*100
    fdark=next((y for y in range(h) if dark(y)>0.30),None)
    fdark=fdark/h*100 if fdark is not None else None
    # logo zone top-left on a (assumed navy) cover
    light=sum(1 for y in range(int(h*0.015),int(h*0.075)) for x in range(int(w*0.02),int(w*0.24))
              if px[x,y][0]>150 and px[x,y][1]>180 and px[x,y][2]>180)
    iscover = fdark is not None and fdark<2.0
    issues=[]
    if topwhite>1.2: issues.append(f"TOP-WHITESPACE {topwhite:.2f}%")
    if iscover and light<40: issues.append("COVER-LOGO-MISSING")
    status='OK' if not issues else 'FAIL: '+', '.join(issues)
    print(f"{label:26} topwhite={topwhite:.2f}% darkband={('%.2f%%'%fdark) if fdark is not None else 'none':>7} logoPx={light:4d}  {status}")
    return not issues
if __name__=='__main__':
    ok=check(sys.argv[1], sys.argv[2] if len(sys.argv)>2 else sys.argv[1].split('/')[-1])
    sys.exit(0 if ok else 1)
