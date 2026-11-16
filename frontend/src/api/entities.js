import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
export const reviews={
 list:()=>api('/review/plans'),
 create:data=>api('/review/plans',{method:'POST',body:JSON.stringify(data)}),
 update:(id,data)=>api('/review/plans/'+id,{method:'PATCH',body:JSON.stringify(data)}),
 toggle:(id,enabled)=>api('/review/plans/'+id+'/toggle',{method:'POST',body:JSON.stringify({enabled})}),
 saveLine:(id,data)=>api('/review/plans/'+id+'/lines',{method:'POST',body:JSON.stringify(data)}),
 disableLine:(id,line)=>api('/review/plans/'+id+'/lines/'+line,{method:'DELETE'}),
 generate:id=>api('/review/plans/'+id+'/generate',{method:'POST'}),
 periods:status=>api('/review/periods'+(status?'?status='+status:'')),
 summary:()=>api('/review/periods/summary'),
 period:id=>api('/review/periods/'+id),
 approve:id=>api('/review/periods/'+id+'/approve',{method:'POST'}),
 distribute:id=>api('/review/periods/'+id+'/distribute',{method:'POST'}),
 retry:id=>api('/review/branches/'+id+'/retry',{method:'POST'}),
 fillData:(id,datasetId)=>api('/review/branches/'+id+'/data',{method:'POST',body:JSON.stringify({dataset_id:datasetId})}),
 deliveries:()=>api('/review/deliveries')
};
