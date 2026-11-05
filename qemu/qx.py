"""Drive the running spike QEMU: qx.py key 0x13:0x04 | knob N | push | shot NAME | rd ADDR | jog N"""
import os, sys, time, socket, re
R=os.path.expanduser("~/Downloads/ref/cdj-qemu-spike"); sys.path.insert(0,R+"/scripts/run")
import cdj_monsock
from snapshot_deck import screendump
MON="/tmp/spike-mon.sock"
def send(msg):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s: s.sendto(msg.encode(), ("127.0.0.1",40124))
def rd(a, n=4):
    out=cdj_monsock.command(MON, f"xp /1{'wbh'[[4,1,2].index(n)] if n!=4 else 'w'}x {a:#x}").decode("utf-8","replace")
    m=re.search(r":\s*0x([0-9a-f]+)", out); return int(m.group(1),16)
a=sys.argv[1:]
while a:
    c=a.pop(0)
    if c=="key": send(f"{a.pop(0)}:300"); time.sleep(1.5)
    elif c=="push": send("0x11:0x01:300"); time.sleep(2)
    elif c=="knob":
        n=int(a.pop(0)); 
        for i in range(abs(n)): send(f"{0x0B6C7B48}:{1 if n>0 else -1}:0:m16add"); time.sleep(0.12)
        time.sleep(1)
    elif c=="wait": time.sleep(float(a.pop(0)))
    elif c=="shot":
        p=f"/tmp/qx_{a.pop(0)}.ppm"; screendump(MON,p)
        from PIL import Image; Image.open(p).save(p[:-4]+".png"); print("shot",p[:-4]+".png")
    elif c=="rd": x=int(a.pop(0),0); print(hex(x), hex(rd(x)))
    elif c=="pos": print("pos", rd(0x0C0E6D18), "mode", rd(0x0B6C4264)&0xff)
