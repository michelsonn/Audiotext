from pathlib import Path
from audiotext.engine import SegmentData, segments_to_srt, parse_srt, atomic_write_text

def test_srt_roundtrip(tmp_path: Path):
    p=tmp_path/'x.srt'; atomic_write_text(p, segments_to_srt([SegmentData(1,0.0,1.2,'Привет')]))
    x=parse_srt(p); assert len(x)==1 and x[0].text=='Привет'
