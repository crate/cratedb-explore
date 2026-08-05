CREATE TABLE IF NOT EXISTS doc.cpu ( 
 "tags" OBJECT(DYNAMIC) AS ( 
   "arch" TEXT, 
   "datacenter" TEXT, 
   "hostname" TEXT, 
   "os" TEXT, 
   "rack" TEXT, 
   "region" TEXT, 
   "service" TEXT, 
   "service_environment" TEXT, 
   "service_version" TEXT, 
   "team" TEXT 
 ), 
 "ts" TIMESTAMP WITH TIME ZONE, 
 "usage_user" INTEGER, 
 "usage_system" INTEGER, 
 "usage_idle" INTEGER, 
 "usage_nice" INTEGER, 
 "usage_iowait" INTEGER, 
 "usage_irq" INTEGER, 
 "usage_softirq" INTEGER, 
 "usage_steal" INTEGER, 
 "usage_guest" INTEGER, 
 "usage_guest_nice" INTEGER 
) 
CLUSTERED INTO <number of shards> SHARDS 
WITH (number_of_replicas = <number of replicas>);
