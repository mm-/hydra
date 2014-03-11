#! /usr/bin/env python
import argparse
from compare_collections import MismatchLogger
from faster_ordered_dict import FasterOrderedDict
import gevent
import gevent.monkey
from gevent.pool import Pool
from pymongo.read_preferences import ReadPreference
import time
import utils

log = utils.get_logger(__name__)

POOL_SIZE = 20

class Stats(object):
    def __init__(self):
        self.start_time = time.time()
        self.processed = 0
        self.not_found = 0
        self.total = None

    def log(self):
        log.info("%d / %d processed | %d not found", stats.processed, stats.total, stats.not_found)

def copy_document_worker(query_doc, source_collection, dest_collection, stats):
    """
    greenlet function that copies a document identified by the query document

    there is a *very* narrow race condition where the document might be deleted from the source
    between our find() and save(); that seems an acceptable risk
    """
    docs = [doc for doc in source_collection.find(query_doc)]
    assert len(docs) <= 1
    if len(docs) == 0:
        # if the document has been deleted from the source, we assume that the oplog applier
        # will delete from the destination in the future
        stats.not_found += 1
        stats.processed += 1
    else:
        # we have the document, so copy it
        dest_collection.save(docs[0])
        stats.processed +=1


def stats_worker(stats):
    """
    prints stats periodically
    """
    while True:
        gevent.sleep(3)
        stats.log()


if __name__ == '__main__':
    utils.tune_gc()
    gevent.monkey.patch_socket()

    parser = argparse.ArgumentParser(description='Through stdin, reads JSON documents containing _ids and shark keys for mismatching documents and re-copies those documents.')
    parser.add_argument(
        '--source', type=str, required=True, metavar='URL',
        help='source to read from; e.g. localhost:27017/prod_maestro.emails')
    parser.add_argument(
        '--dest', type=str, required=True, metavar='URL',
        help='destination to copy to; e.g. localhost:27017/destination_db.emails')
    parser.add_argument(
        '--user', type=str, required=False, metavar='USER',
        help='User to auth with, not required')
    parser.add_argument(
        '--password', type=str, required=False, metavar='PASSWORD',
        help='Pass word for the auth to use')
    parser.add_argument(
        '--authDB', type=str, required=False, metavar='AUTHDB',
        help='Database to auth against')
    parser.add_argument(
        '--mismatches-file', type=str, default=None, required=True, metavar='FILENAME',
        help='read ids to copy from this file, which is generated by compare_collections.py')
    args = parser.parse_args()

    # connect to source and destination
    source = utils.parse_mongo_url(args.source)
    source['user'] = args.user if 'user' in args else False
    source['password'] = args.password if 'password' in args else False
    source['authDB'] = args.authDB if 'authDB' in args else False
    source_client = utils.mongo_connect(source['host'], source['port'],
                                        source['user'], source['password'], source['authDB'],
                                        ensure_direct=True,
                                        max_pool_size=POOL_SIZE,
                                        read_preference=ReadPreference.SECONDARY_PREFERRED,
                                        document_class=FasterOrderedDict)

    source_collection = source_client[source['db']][source['collection']]
    if not source_client.is_mongos or not source_client.is_primary:
        raise Exception("source must be a mongos instance or a primary")


    dest = utils.parse_mongo_url(args.dest)
    dest['user'] = args.user if 'user' in args else False
    dest['password'] = args.password if 'password' in args else False
    dest['authDB'] = args.authDB if 'authDB' in args else False
    dest_client = utils.mongo_connect(dest['host'], dest['port'],
                                      dest['user'], dest['password'], dest['authDB'],
                                      max_pool_size=POOL_SIZE,
                                      document_class=FasterOrderedDict)

    dest_collection = dest_client[dest['db']][dest['collection']]

    if source == dest:
        raise ValueError("source and destination cannot be the same!")

    # periodically print stats
    stats = Stats()
    stats_greenlet = gevent.spawn(stats_worker, stats)

    # copy documents!
    pool = Pool(POOL_SIZE)
    with open(args.mismatches_file) as mismatches_file:
        lines = mismatches_file.readlines()  # copy everything into memory -- hopefully that isn't huge
    stats.total = len(lines)
    for line in lines:
        query_doc = {'_id': MismatchLogger.decode_mismatch_id(line)}
	print(query_doc)
        pool.spawn(copy_document_worker,
                   query_doc=query_doc,
                   source_collection=source_collection,
                   dest_collection=dest_collection,
                   stats=stats)

    # wait for everythng to finish
    gevent.sleep()
    pool.join()
    stats_greenlet.kill()
    stats.log()
    log.info('done')
