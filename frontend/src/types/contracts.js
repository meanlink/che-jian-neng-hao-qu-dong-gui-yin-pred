/** @typedef {{id:number,name:string,source:string,rows:number,checksum:string}} Dataset */
/** @typedef {{id:number,name:string,dataset_id:number,status:string,result:Object,approved:boolean}} AnalysisJob */
/** @typedef {{id:number,name:string,job_id:number,destination:string,next_run:number,enabled:boolean}} ReportSchedule */
/** @typedef {{id:number,name:string,weekdays:number[],run_time:string,start_date:string,holidays:string[],enabled:boolean,lines:Array}} ReviewPlan */
/** @typedef {{id:number,plan_id:number,due_date:string,status:string,briefing:Object,lines:Array}} ReviewPeriod */
export {};
