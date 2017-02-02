from pyspark.mllib.classification import LogisticRegressionWithLBFGS
from pyspark.mllib.regression import LabeledPoint
from pyspark.sql import SparkSession
from pyspark import SparkContext
import numpy as np

sc = SparkContext('local', 'logistic')
spark = SparkSession \
    .builder \
    .appName("Logistic regression w/ Spark ML") \
    .getOrCreate()

BUCKET='cloud-training-demos-ml'

# read dataset
traindays = spark.read \
    .option("header", "true") \
    .csv('gs://cloud-training-demos-ml/flights/trainday.csv')
traindays.createOrReplaceTempView('traindays')

from pyspark.sql.types import StringType, FloatType, StructType, StructField

header = 'FL_DATE,UNIQUE_CARRIER,AIRLINE_ID,CARRIER,FL_NUM,ORIGIN_AIRPORT_ID,ORIGIN_AIRPORT_SEQ_ID,ORIGIN_CITY_MARKET_ID,ORIGIN,DEST_AIRPORT_ID,DEST_AIRPORT_SEQ_ID,DEST_CITY_MARKET_ID,DEST,CRS_DEP_TIME,DEP_TIME,DEP_DELAY,TAXI_OUT,WHEELS_OFF,WHEELS_ON,TAXI_IN,CRS_ARR_TIME,ARR_TIME,ARR_DELAY,CANCELLED,CANCELLATION_CODE,DIVERTED,DISTANCE,DEP_AIRPORT_LAT,DEP_AIRPORT_LON,DEP_AIRPORT_TZOFFSET,ARR_AIRPORT_LAT,ARR_AIRPORT_LON,ARR_AIRPORT_TZOFFSET,EVENT,NOTIFY_TIME'

def get_structfield(colname):
   if colname in ['ARR_DELAY', 'DEP_DELAY', 'DISTANCE', 'TAXI_OUT', 'DEP_AIRPORT_TZOFFSET']:
      return StructField(colname, FloatType(), True)
   else:
      return StructField(colname, StringType(), True)

schema = StructType([get_structfield(colname) for colname in header.split(',')])


#inputs = 'gs://cloud-training-demos-ml/flights/tzcorr/all_flights-00000-*' # 1/30th
inputs = 'gs://cloud-training-demos-ml/flights/tzcorr/all_flights-*'  # FULL
flights = spark.read\
            .schema(schema)\
            .csv(inputs)
flights.createOrReplaceTempView('flights')

# separate training and validation data
from pyspark.sql.functions import rand
SEED=13
traindays = traindays.withColumn("holdout", rand(SEED) > 0.8)  # 80% of data is for training
traindays.createOrReplaceTempView('traindays')



# logistic regression
trainquery = """
SELECT
  *
FROM flights f
JOIN traindays t
ON f.FL_DATE == t.FL_DATE
WHERE
  t.is_train_day == 'True' AND
  t.holdout == False AND
  f.CANCELLED == '0.00' AND 
  f.DIVERTED == '0.00'
"""
traindata = spark.sql(trainquery).repartition(1000)

def get_category(hour):
  if hour < 6 or hour > 20:
     return [1, 0, 0, 0]  # night
  if hour < 10:
     return [0, 1, 0, 0] # morning
  if hour < 5:
     return [0, 0, 1, 0] # mid-day
  else:
     return [0, 0, 0, 1] # evening

def get_local_hour(timestamp, correction):
      import datetime
      TIME_FORMAT = '%Y-%m-%d %H:%M:%S'
      timestamp = timestamp.replace('T', ' ') # incase different
      t = datetime.datetime.strptime(timestamp, TIME_FORMAT)
      d = datetime.timedelta(seconds=correction)
      t = t + d
      #return t.hour    # raw
      #theta = np.radians(360 * t.hour / 24.0)  # von-Miyes
      #return [np.sin(theta), np.cos(theta)]
      return get_category(t.hour)   # bucketize

def to_example(fields):
  features = [ \
                  fields['DEP_DELAY'], \
                  fields['TAXI_OUT'], \
             ]
  features.extend(get_local_hour(fields['DEP_TIME'],
                          fields['DEP_AIRPORT_TZOFFSET']))

  return LabeledPoint(\
              float(fields['ARR_DELAY'] < 15), #ontime \
              features)

def add_hour(df):
   from pyspark.sql.functions import udf
   from pyspark.ml.feature import OneHotEncoder
   hour_encoder = OneHotEncoder(inputCol="HOUR", outputCol="HOUR_onehot")
   udf_get_local_hour = udf( lambda (t, c): get_local_hour(t,c) )
   with_hour = df.withColumn('HOUR', 
                   udf_get_local_hour(traindata['DEP_TIME'],
                                      traindata['DEP_AIRPORT_TZOFFSET']))
   with_hour = with_hour.withColumn("HOUR", with_hour["HOUR"].cast("int"))
   return hour_encoder.transform(with_hour)

#traindata = add_hour(traindata)
examples = traindata.rdd.map(to_example)
lrmodel = LogisticRegressionWithLBFGS.train(examples, intercept=True)
lrmodel.setThreshold(0.7)

# save model
MODEL_FILE='gs://' + BUCKET + '/flights/sparkmloutput/model'
lrmodel.save(sc, MODEL_FILE)


# evaluate model on the heldout data
evalquery = trainquery.replace("t.holdout == False","t.holdout == True")
evaldata = spark.sql(evalquery).repartition(1000)
#evaldata = add_hour(evaldata)
examples = evaldata.rdd.map(to_example)
labelpred = examples.map(lambda p: (p.label, lrmodel.predict(p.features)))
def eval(labelpred):
    cancel = labelpred.filter(lambda (label, pred): pred == 0)
    nocancel = labelpred.filter(lambda (label, pred): pred == 1)
    corr_cancel = cancel.filter(lambda (label, pred): label == pred).count()
    corr_nocancel = nocancel.filter(lambda (label, pred): label == pred).count()
    totsqe = labelpred.map(lambda (label, pred): (label-pred)*(label-pred)).sum()

    cancel_denom = cancel.count()
    nocancel_denom = nocancel.count()
    if cancel_denom == 0:
        cancel_denom = 1
    if nocancel_denom == 0:
        nocancel_denom = 1

    return {'total_cancel': cancel.count(), \
            'correct_cancel': float(corr_cancel)/cancel_denom, \
            'total_noncancel': nocancel.count(), \
            'correct_noncancel': float(corr_nocancel)/nocancel_denom, \
            'rmse': np.sqrt(totsqe/float(cancel_denom + nocancel_denom))
           }
print eval(labelpred)
