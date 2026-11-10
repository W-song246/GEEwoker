"""同一网格行中的配对样本统计，不配对两列独立随机样本。"""
import math


def summarize_pairs(rows):
    pairs=[{'ndvi':float(r['NDVI']),'temperature':float(r['LST_C'])} for r in rows
           if r.get('NDVI') is not None and r.get('LST_C') is not None
           and math.isfinite(r['NDVI']) and math.isfinite(r['LST_C'])]
    n=len(pairs);result={'pairs':pairs,'sample_count':n,'status':'insufficient','bins':[],'pearson_r':None,'slope_c_per_ndvi':None}
    if not pairs:return result
    ordered=sorted(pairs,key=lambda r:r['ndvi'])
    result['ndvi_median']=ordered[n//2]['ndvi'];result['temperature_median']=sorted(r['temperature'] for r in pairs)[n//2]
    if n<10:return result
    x=sum(p['ndvi'] for p in pairs)/n;y=sum(p['temperature'] for p in pairs)/n
    xx=sum((p['ndvi']-x)**2 for p in pairs);yy=sum((p['temperature']-y)**2 for p in pairs)
    if xx<1e-12 or yy<1e-12:return result
    xy=sum((p['ndvi']-x)*(p['temperature']-y) for p in pairs)
    result.update(status='ready',pearson_r=max(-1,min(1,xy/math.sqrt(xx*yy))),
                  slope_c_per_ndvi=xy/xx,intercept_c=y-xy/xx*x)
    if n>=20:
        for i in range(4):
            group=ordered[i*n//4:(i+1)*n//4]
            result['bins'].append({'label':f"第{i+1}组 NDVI {group[0]['ndvi']:.3f}–{group[-1]['ndvi']:.3f}",
                                   'temperatures':[p['temperature'] for p in group], 'count':len(group)})
    return result
