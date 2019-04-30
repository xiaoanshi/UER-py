# -*- encoding:utf-8 -*-
import os
import torch
import codecs
import random
import pickle
from multiprocessing import Pool
from uer.utils.constants import *
from uer.utils.seed import set_seed


def mask_seq(src, vocab_size):
        """
        mask input sequence for MLM task
        args:
            src: a list of tokens
        """
        tgt_mlm = []
        for (i, token) in enumerate(src):
            if token == CLS_ID or token == SEP_ID:
                tgt_mlm.append(PAD_ID)
                continue
            prob = random.random()
            if prob < 0.15:
                prob /= 0.15
                if prob < 0.8:
                    src[i] = MASK_ID
                elif prob < 0.9:
                    while True:
                        rdi = random.randint(1, vocab_size-1)
                        if rdi not in [CLS_ID, SEP_ID, MASK_ID]:
                            break
                    src[i] = rdi
                tgt_mlm.append(token)
            else:
                tgt_mlm.append(PAD_ID)
        return src, tgt_mlm


def count_lines(file_path):
    lines_num = 0
    with open(file_path, mode="r", encoding="utf-8") as f:
        for line in f:
            lines_num += 1
    return lines_num


class BertDataset(object):
    """
    Construct dataset for MLM and NSP tasks from the given corpus.
    Each document consists of multiple sentences, 
    and each sentence occupies a single line. 
    Documents in corpus must be separated by empty lines.
    """
    def __init__(self, args, vocab, tokenizer):
        self.vocab = vocab
        self.tokenizer = tokenizer
        self.corpus_path = args.corpus_path
        self.dataset_path = args.dataset_path

        self.buffer_size = args.docs_buffer_size        
        self.seq_length = args.seq_length
        self.dup_factor = args.dup_factor
        self.short_seq_prob = args.short_seq_prob
        self.seed = args.seed

    def build_and_save(self, workers_num):
        """
        Build dataset from the given corpus.
        Start workers_num processes and each process deals with a part of data.
        """
        lines_num = count_lines(self.corpus_path)
        print("Starting %d workers for building datasets ... " % workers_num)
        assert(workers_num >= 1)
        if workers_num == 1:
            self.worker(0, 0, lines_num)
        else:
            pool = Pool(workers_num)
            for i in range(workers_num):
                start = i * lines_num // workers_num
                end = (i+1) * lines_num // workers_num
                pool.apply_async(func=self.worker, args=[i, start, end])
            pool.close()
            pool.join()

    def worker(self, proc_id, start, end):
        print("Worker %d is building dataset ... " % proc_id)
        set_seed(self.seed)
        docs_buffer = []
        document = []
        pos = 0
        f_write = open(self.dataset_path + "-" + str(proc_id) + ".pt", "wb")
        with open(self.corpus_path, mode="r", encoding="utf-8") as f:
            while pos < start:
                try:
                    f.readline()
                except:
                    continue
                finally:
                    pos += 1
            while True:
                try:
                    line = f.readline()
                except:
                    continue
                finally:
                    pos += 1
                if not line.strip():
                    if len(document) >= 1:
                        docs_buffer.append(document)
                    document = []
                    if len(docs_buffer) == self.buffer_size:
                        # Build instances from documents.                    
                        instances = self.build_instances(docs_buffer)
                        # Save instances.
                        pickle.dump(instances, f_write)
                        print("Worker {:d}, process: {:.1f}%".format(proc_id, (pos-start)/(end-start)*100), end="\r")
                        # Clear buffer.
                        docs_buffer = []
                        instances = []
                    continue
                sentence = [self.vocab.get(w) for w in self.tokenizer.tokenize(line)]
                if len(sentence) > 0:
                    document.append(sentence)
        
                if pos >= end - 1:
                    if len(docs_buffer) > 0:
                        instances = self.build_instances(docs_buffer)
                        pickle.dump(instances, f_write)
                    break
        f_write.close()

    def build_instances(self, all_documents):
        instances = []
        for _ in range(self.dup_factor):
            for doc_index in range(len(all_documents)):
                instances.extend(self.create_ins_from_doc(all_documents, doc_index))
        return instances

    def create_ins_from_doc(self, all_documents, document_index):
        document = all_documents[document_index]
        max_num_tokens = self.seq_length - 3
        target_seq_length = max_num_tokens
        if random.random() < self.short_seq_prob:
            target_seq_length = random.randint(2, max_num_tokens)
        instances = []
        current_chunk = []
        current_length = 0
        i = 0
        while i < len(document):
            segment = document[i]
            current_chunk.append(segment)
            current_length += len(segment)
            if i == len(document) - 1 or current_length >= target_seq_length:
                if current_chunk:
                    a_end = 1
                    if len(current_chunk) >= 2:
                        a_end = random.randint(1, len(current_chunk) - 1)

                    tokens_a = []
                    for j in range(a_end):
                        tokens_a.extend(current_chunk[j])

                    tokens_b = []
                    is_random_next = 0

                    # Random next
                    if len(current_chunk) == 1 or random.random() < 0.5:
                        is_random_next = 1
                        target_b_length = target_seq_length - len(tokens_a)

                        for _ in range(10):
                            random_document_index = random.randint(0, len(all_documents) - 1)
                            if random_document_index != document_index:
                                break

                        random_document = all_documents[random_document_index]
                        random_start = random.randint(0, len(random_document) - 1)
                        for j in range(random_start, len(random_document)):
                            tokens_b.extend(random_document[j])
                            if len(tokens_b) >= target_b_length:
                                break

                        num_unused_segments = len(current_chunk) - a_end
                        i -= num_unused_segments

                    # Actual next
                    else:
                        is_random_next = 0
                        for j in range(a_end, len(current_chunk)):
                            tokens_b.extend(current_chunk[j])

                    self.truncate_seq_pair(tokens_a, tokens_b, max_num_tokens)

                    # assert len(tokens_a) >= 1
                    # assert len(tokens_b) >= 1

                    src = []
                    seg = []
                    src.append(CLS_ID)
                    seg.append(1)
                    for token in tokens_a:
                      src.append(token)
                      seg.append(1)

                    src.append(SEP_ID)
                    seg.append(1)

                    for token in tokens_b:
                        src.append(token)
                        seg.append(2)
                    src.append(SEP_ID)
                    seg.append(2)

                    src, tgt_mlm = mask_seq(src, len(self.vocab))
                    
                    while len(src) != self.seq_length:
                        src.append(PAD_ID)
                        tgt_mlm.append(PAD_ID)
                        seg.append(PAD_ID)

                    instance = (src, tgt_mlm, is_random_next, seg)
                    instances.append(instance)
                current_chunk = []
                current_length = 0
            i += 1
        return instances

    def truncate_seq_pair(self, tokens_a, tokens_b, max_num_tokens):
        """ truncate sequence pair to specific length """
        while True:
            total_length = len(tokens_a) + len(tokens_b)
            if total_length <= max_num_tokens:
                break
                
            trunc_tokens = tokens_a if len(tokens_a) > len(tokens_b) else tokens_b
            # assert len(trunc_tokens) >= 1

            if random.random() < 0.5:
                del trunc_tokens[0]
            else:
                trunc_tokens.pop()


class BertDataLoader(object):
    """
    """
    def __init__(self, args, dataset_path, batch_size, proc_id, proc_num, shuffle=True):
        self.batch_size = batch_size
        self.proc_id = proc_id
        self.proc_num = proc_num
        self.shuffle = shuffle
        self.buffer_size = args.instances_buffer_size
        # We only need to read dataset once when buffer is big enough to load entire dataset.
        self.repeat_read_dataset = False
        self.f_read = open(dataset_path, "rb")
        self.start = 0
        self.end = 0
        self.buffer = []
        
    def _fill_buf(self):
        if len(self.buffer) > 0 and not self.repeat_read_dataset:
            if self.shuffle:
                random.shuffle(self.buffer)
            self.start = 0
            self.end = len(self.buffer)
        else:
            self.buffer = []
            while True:
                try:
                    instances = pickle.load(self.f_read)
                except EOFError:
                    # Reach file end.
                    if not self.repeat_read_dataset:
                        # Buffer is big enough to load entire dataset.
                        break
                    # Buffer is not big enough, read dataset form start.
                    self.f_read.seek(0)
                    instances = pickle.load(self.f_read)

                self.buffer.extend(instances)                
                if len(self.buffer) > self.buffer_size:
                    self.repeat_read_dataset = True 
                    break

            if self.shuffle:
                random.shuffle(self.buffer)
            self.start = 0
            self.end = len(self.buffer)

    def _empty(self):
        return self.start + self.batch_size*self.proc_num >= self.end

    def __del__(self):
        self.f_read.close()

    def __iter__(self):
        while True:
            if self._empty():
                self._fill_buf()
            instances = self.buffer[self.start + self.proc_id*self.batch_size: self.start + (self.proc_id+1)*self.batch_size]
            self.start += self.batch_size*self.proc_num
        
            src = []
            tgt_mlm = []
            is_next = []
            seg = []
            for ins in instances:
                src.append(ins[0])
                tgt_mlm.append(ins[1])
                is_next.append(ins[2])
                seg.append(ins[3])

            yield torch.LongTensor(src), \
                torch.LongTensor(tgt_mlm), \
                torch.LongTensor(is_next), \
                torch.LongTensor(seg)


class LmDataset(object):
    """
    Construct dataset for MLM and NSP tasks from the given corpus.
    Each document consists of multiple sentences, 
    and each sentence occupies a single line. 
    Documents in corpus must be separated by empty lines.
    """
    def __init__(self, args, vocab, tokenizer):
        self.vocab = vocab
        self.tokenizer = tokenizer
        self.corpus_path = args.corpus_path
        self.dataset_path = args.dataset_path
        
        self.seq_length = args.seq_length
        self.seed = args.seed

    def build_and_save(self, workers_num):
        """
        Build dataset from the given corpus.
        Start workers_num processes and each process deals with a part of data.
        """
        file_size = os.path.getsize(self.corpus_path)
        print("Starting %d workers for building datasets ... " % workers_num)
        assert(workers_num >= 1)
        if workers_num == 1:
            self.worker(0, 0, file_size)
        else:
            pool = Pool(workers_num)
            for i in range(workers_num):
                start = i * file_size // workers_num
                end = (i+1) * file_size // workers_num
                pool.apply_async(func=self.worker, args=[i, start, end])
            pool.close()
            pool.join()

    def worker(self, proc_id, start, end):
        print("Worker %d is building dataset ... " % proc_id)
        set_seed(self.seed)
        docs_buffer = []
        document = []
        pos = start
        f_write = open(self.dataset_path + "-" + str(proc_id) + ".pt", "wb")
        # Open function in python3 does not support tell operation. We have to use codecs.
        with codecs.open(self.corpus_path, "r", "utf-8") as f:
            f.seek(start)
            instances = []
            while True:
                try:
                    line = f.readline()
                except UnicodeDecodeError:
                    continue

                src = [self.vocab.get(w) for w in self.tokenizer.tokenize(line)]
                tgt = src[1:]
                src = src[:-1]
                seg = [1] * len(src)
                if len(src) >= self.seq_length:
                    src = src[:self.seq_length]
                    tgt = tgt[:self.seq_length]
                    seg = seg[:self.seq_length]
                else:
                    while len(src) != self.seq_length:
                        src.append(PAD_ID)
                        tgt.append(PAD_ID)
                        seg.append(PAD_ID)

                instances.append((src, tgt, seg))

                pos = f.tell()
                if pos >= end:
                    pickle.dump(instances, f_write)
                    break

        f_write.close()


class LmDataLoader(object):
    """
    """
    def __init__(self, args, dataset_path, batch_size, proc_id, proc_num, shuffle=True):
        self.batch_size = batch_size
        self.shuffle = shuffle
        self.proc_id = proc_id
        self.proc_num = proc_num

        self.f_read = open(dataset_path, "rb")
        self.start = 0
        self.end = 0
        self.buffer = []
        
    def _fill_buf(self):
        try:
            self.buffer = pickle.load(self.f_read)
        except EOFError:
            # Reach file end.
            self.f_read.seek(0)
            self.buffer = pickle.load(self.f_read)

        if self.shuffle:
            random.shuffle(self.buffer)
        self.start = 0
        self.end = len(self.buffer)

    def _empty(self):
        return self.start + self.batch_size*self.proc_num >= self.end

    def __del__(self):
        self.f_read.close()

    def __iter__(self):
        while True:
            if self._empty():
                self._fill_buf()
            instances = self.buffer[self.start + self.proc_id*self.batch_size: self.start + (self.proc_id+1)*self.batch_size]
            self.start += self.batch_size*self.proc_num
        
            src = []
            tgt = []
            seg = []

            for ins in instances:
                src.append(ins[0])
                tgt.append(ins[1])
                seg.append(ins[2])

            yield torch.LongTensor(src), \
                torch.LongTensor(tgt), \
                torch.LongTensor(seg)


class ClsDataset(object):
    """
    Construct dataset for MLM and NSP tasks from the given corpus.
    Each document consists of multiple sentences, 
    and each sentence occupies a single line. 
    Documents in corpus must be separated by empty lines.
    """
    def __init__(self, args, vocab, tokenizer):
        self.vocab = vocab
        self.tokenizer = tokenizer
        self.corpus_path = args.corpus_path
        self.dataset_path = args.dataset_path
        
        self.seq_length = args.seq_length
        self.seed = args.seed

    def build_and_save(self, workers_num):
        """
        Build dataset from the given corpus.
        Start workers_num processes and each process deals with a part of data.
        """
        file_size = os.path.getsize(self.corpus_path)
        print("Starting %d workers for building datasets ... " % workers_num)
        assert(workers_num >= 1)
        if workers_num == 1:
            self.worker(0, 0, file_size)
        else:
            pool = Pool(workers_num)
            for i in range(workers_num):
                start = i * file_size // workers_num
                end = (i+1) * file_size // workers_num
                pool.apply_async(func=self.worker, args=[i, start, end])
            pool.close()
            pool.join()

    def worker(self, proc_id, start, end):
        print("Worker %d is building dataset ... " % proc_id)
        set_seed(self.seed)
        pos = start
        f_write = open(self.dataset_path + "-" + str(proc_id) + ".pt", "wb")
        # Open function in python3 does not support tell operation. We have to use codecs.
        with codecs.open(self.corpus_path, "r", "utf-8") as f:
            f.seek(start)
            instances = []
            while True:
                try:
                    line = f.readline()
                    line = line.strip().split("\t")
                    label, text = int(line[0]), line[1]
                except:
                    continue

                src = [self.vocab.get(w) for w in self.tokenizer.tokenize(text)]
                tgt = label
                seg = [1] * len(src)
                if len(src) >= self.seq_length:
                    src = src[:self.seq_length]
                    seg = seg[:self.seq_length]
                else:
                    while len(src) != self.seq_length:
                        src.append(PAD_ID)
                        seg.append(PAD_ID)

                instances.append((src, tgt, seg))

                pos = f.tell()
                if pos >= end:
                    pickle.dump(instances, f_write)
                    break

        f_write.close()


class ClsDataLoader(object):
    """
    """
    def __init__(self, args, dataset_path, batch_size, proc_id, proc_num, shuffle=True):
        self.batch_size = batch_size
        self.shuffle = shuffle
        self.proc_id = proc_id
        self.proc_num = proc_num

        self.f_read = open(dataset_path, "rb")
        self.start = 0
        self.end = 0
        self.buffer = []
        
    def _fill_buf(self):

        try:
            self.buffer = pickle.load(self.f_read)
        except EOFError:
            # Reach file end.
            self.f_read.seek(0)
            self.buffer = pickle.load(self.f_read)

        if self.shuffle:
            random.shuffle(self.buffer)
        self.start = 0
        self.end = len(self.buffer)

    def _empty(self):
        return self.start + self.batch_size*self.proc_num >= self.end

    def __del__(self):
        self.f_read.close()

    def __iter__(self):
        while True:
            if self._empty():
                self._fill_buf()
            instances = self.buffer[self.start + self.proc_id*self.batch_size: self.start + (self.proc_id+1)*self.batch_size]
            self.start += self.batch_size*self.proc_num
        
            src = []
            tgt = []
            seg = []

            for ins in instances:
                src.append(ins[0])
                tgt.append(ins[1])
                seg.append(ins[2])

            yield torch.LongTensor(src), \
                torch.LongTensor(tgt), \
                torch.LongTensor(seg)


class MlmDataset(object):
    """
    Construct dataset for MLM and NSP tasks from the given corpus.
    Each document consists of multiple sentences, 
    and each sentence occupies a single line. 
    Documents in corpus must be separated by empty lines.
    """
    def __init__(self, args, vocab, tokenizer):
        self.vocab = vocab
        self.tokenizer = tokenizer
        self.corpus_path = args.corpus_path
        self.dataset_path = args.dataset_path
        
        self.seq_length = args.seq_length
        self.seed = args.seed
        self.dup_factor = args.dup_factor

    def build_and_save(self, workers_num):
        """
        Build dataset from the given corpus.
        Start workers_num processes and each process deals with a part of data.
        """
        file_size = os.path.getsize(self.corpus_path)
        print("Starting %d workers for building datasets ... " % workers_num)
        assert(workers_num >= 1)
        if workers_num == 1:
            self.worker(0, 0, file_size)
        else:
            pool = Pool(workers_num)
            for i in range(workers_num):
                start = i * file_size // workers_num
                end = (i+1) * file_size // workers_num
                pool.apply_async(func=self.worker, args=[i, start, end])
            pool.close()
            pool.join()

    def worker(self, proc_id, start, end):
        print("Worker %d is building dataset ... " % proc_id)
        set_seed(self.seed)
        pos = start
        f_write = open(self.dataset_path + "-" + str(proc_id) + ".pt", "wb")
        # Open function in python3 does not support tell operation. We have to use codecs.
        with codecs.open(self.corpus_path, "r", "utf-8") as f:
            instances = []
            for _ in range(self.dup_factor):
                f.seek(start)
                while True:
                    try:
                        line = f.readline()
                    except:
                        continue

                    src = [self.vocab.get(w) for w in self.tokenizer.tokenize(line)]
                    src, tgt = mask_seq(src, len(self.vocab))
                    seg = [1] * len(src)
                    if len(src) >= self.seq_length:
                        src = src[:self.seq_length]
                        tgt = tgt[:self.seq_length]
                        seg = seg[:self.seq_length]
                    else:
                        while len(src) != self.seq_length:
                            src.append(PAD_ID)
                            tgt.append(PAD_ID)
                            seg.append(PAD_ID)

                    instances.append((src, tgt, seg))

                    pos = f.tell()
                    if pos >= end:
                        break

        pickle.dump(instances, f_write)
        f_write.close()


class MlmDataLoader(object):
    """
    """
    def __init__(self, args, dataset_path, batch_size, proc_id, proc_num, shuffle=True):
        self.batch_size = batch_size
        self.shuffle = shuffle
        self.proc_id = proc_id
        self.proc_num = proc_num

        self.f_read = open(dataset_path, "rb")
        self.start = 0
        self.end = 0
        self.buffer = []
        
    def _fill_buf(self):

        try:
            self.buffer = pickle.load(self.f_read)
        except EOFError:
            # Reach file end.
            self.f_read.seek(0)
            self.buffer = pickle.load(self.f_read)

        if self.shuffle:
            random.shuffle(self.buffer)
        self.start = 0
        self.end = len(self.buffer)

    def _empty(self):
        return self.start + self.batch_size*self.proc_num >= self.end

    def __del__(self):
        self.f_read.close()

    def __iter__(self):
        while True:
            if self._empty():
                self._fill_buf()
            instances = self.buffer[self.start + self.proc_id*self.batch_size: self.start + (self.proc_id+1)*self.batch_size]
            self.start += self.batch_size*self.proc_num
        
            src = []
            tgt = []
            seg = []

            for ins in instances:
                src.append(ins[0])
                tgt.append(ins[1])
                seg.append(ins[2])

            yield torch.LongTensor(src), \
                torch.LongTensor(tgt), \
                torch.LongTensor(seg)


class NspDataset(object):
    """

    """
    def __init__(self, args, vocab, tokenizer):
        self.vocab = vocab
        self.tokenizer = tokenizer
        self.corpus_path = args.corpus_path
        self.dataset_path = args.dataset_path
       
        self.seq_length = args.seq_length
        self.seed = args.seed

    def build_and_save(self, workers_num):
        """
        Build dataset from the given corpus.
        Start workers_num processes and each process deals with a part of data.
        """
        file_size = os.path.getsize(self.corpus_path)
        print("Starting %d workers for building datasets ... " % workers_num)
        assert(workers_num >= 1)
        if workers_num == 1:
            self.worker(0, 0, file_size)
        else:
            pool = Pool(workers_num)
            for i in range(workers_num):
                start = i * file_size // workers_num
                end = (i+1) * file_size // workers_num
                pool.apply_async(func=self.worker, args=[i, start, end])
            pool.close()
            pool.join()

    def worker(self, proc_id, start, end):
        print("Worker %d is building dataset ... " % proc_id)
        set_seed(self.seed)
        docs_buffer = []
        document = []
        pos = start
        f_write = open(self.dataset_path + "-" + str(proc_id) + ".pt", "wb")
        # Open function in python3 does not support tell operation. We have to use codecs.
        with codecs.open(self.corpus_path, "r", "utf-8") as f:
            f.seek(start)
            while True:
                try:
                    line = f.readline()
                except:
                    continue
                if not line.strip():
                    if len(document) >= 1:
                        docs_buffer.append(document)
                    document = []
                    continue
                sentence = [self.vocab.get(w) for w in self.tokenizer.tokenize(line)]
                document.append(sentence)
        
                pos = f.tell()
                if pos >= end:
                    if len(docs_buffer) > 0:
                        instances = self.build_instances(docs_buffer)
                        pickle.dump(instances, f_write)
                    break
        f_write.close()

    def build_instances(self, all_documents):
        instances = []
        for doc_index in range(len(all_documents)):
            instances.extend(self.create_ins_from_doc(all_documents, doc_index))
        return instances

    def create_ins_from_doc(self, all_documents, document_index):
        document = all_documents[document_index]
        max_num_tokens = self.seq_length - 3
        target_seq_length = max_num_tokens
        instances = []
        current_chunk = []
        current_length = 0
        i = 0
        while i < len(document):
            segment = document[i]
            current_chunk.append(segment)
            current_length += len(segment)
            if i == len(document) - 1 or current_length >= target_seq_length:
                if current_chunk:
                    a_end = 1
                    if len(current_chunk) >= 2:
                        a_end = random.randint(1, len(current_chunk) - 1)

                    tokens_a = []
                    for j in range(a_end):
                        tokens_a.extend(current_chunk[j])

                    tokens_b = []
                    is_random_next = 0

                    # Random next.
                    if len(current_chunk) == 1 or random.random() < 0.5:
                        is_random_next = 1
                        target_b_length = target_seq_length - len(tokens_a)

                        for _ in range(10):
                            random_document_index = random.randint(0, len(all_documents) - 1)
                            if random_document_index != document_index:
                                break

                        random_document = all_documents[random_document_index]
                        random_start = random.randint(0, len(random_document) - 1)
                        for j in range(random_start, len(random_document)):
                            tokens_b.extend(random_document[j])
                            if len(tokens_b) >= target_b_length:
                                break

                        num_unused_segments = len(current_chunk) - a_end
                        i -= num_unused_segments

                    # Actual next.
                    else:
                        is_random_next = 0
                        for j in range(a_end, len(current_chunk)):
                            tokens_b.extend(current_chunk[j])

                    self.truncate_seq_pair(tokens_a, tokens_b, max_num_tokens)

                    assert len(tokens_a) >= 1
                    assert len(tokens_b) >= 1

                    src = []
                    seg = []
                    src.append(CLS_ID)
                    seg.append(1)
                    for token in tokens_a:
                      src.append(token)
                      seg.append(1)

                    src.append(SEP_ID)
                    seg.append(1)

                    for token in tokens_b:
                        src.append(token)
                        seg.append(2)
                    src.append(SEP_ID)
                    seg.append(2)

                    
                    while len(src) != self.seq_length:
                        src.append(PAD_ID)
                        seg.append(PAD_ID)

                    instance = (src, is_random_next, seg)
                    instances.append(instance)
                current_chunk = []
                current_length = 0
            i += 1
        return instances

    def truncate_seq_pair(self, tokens_a, tokens_b, max_num_tokens):
        """ truncate sequence pair to specific length """
        while True:
            total_length = len(tokens_a) + len(tokens_b)
            if total_length <= max_num_tokens:
                break
                
            trunc_tokens = tokens_a if len(tokens_a) > len(tokens_b) else tokens_b
            assert len(trunc_tokens) >= 1

            if random.random() < 0.5:
                del trunc_tokens[0]
            else:
                trunc_tokens.pop()


class NspDataLoader(object):
    """
    """
    def __init__(self, args, dataset_path, batch_size, proc_id, proc_num, shuffle=True):
        self.batch_size = batch_size
        self.shuffle = shuffle
        self.proc_id = proc_id
        self.proc_num = proc_num

        self.f_read = open(dataset_path, "rb")
        self.start = 0
        self.end = 0
        self.buffer = []
        
    def _fill_buf(self):
        try:
            self.buffer = pickle.load(self.f_read)
        except EOFError:
            # Reach file end.
            self.f_read.seek(0)
            self.buffer = pickle.load(self.f_read)

        if self.shuffle:
            random.shuffle(self.buffer)
        self.start = 0
        self.end = len(self.buffer)

    def _empty(self):
        return self.start + self.batch_size*self.proc_num >= self.end

    def __del__(self):
        self.f_read.close()

    def __iter__(self):
        while True:
            if self._empty():
                self._fill_buf()
            instances = self.buffer[self.start + self.proc_id*self.batch_size: self.start + (self.proc_id+1)*self.batch_size]
            self.start += self.batch_size*self.proc_num
        
            src = []
            seg = []
            tgt = []
            for ins in instances:
                src.append(ins[0])
                tgt.append(ins[1])
                seg.append(ins[2])

            yield torch.LongTensor(src), \
                torch.LongTensor(tgt), \
                torch.LongTensor(seg), \


class S2sDataset(object):
    """
    Construct dataset for MLM and NSP tasks from the given corpus.
    Each document consists of multiple sentences, 
    and each sentence occupies a single line. 
    Documents in corpus must be separated by empty lines.
    """
    def __init__(self, args, vocab, tokenizer):
        self.vocab = vocab
        self.tokenizer = tokenizer
        self.corpus_path = args.corpus_path
        self.dataset_path = args.dataset_path
        
        self.seq_length = args.seq_length
        self.seed = args.seed

    def build_and_save(self, workers_num):
        """
        Build dataset from the given corpus.
        Start workers_num processes and each process deals with a part of data.
        """
        file_size = os.path.getsize(self.corpus_path)
        print("Starting %d workers for building datasets ... " % workers_num)
        assert(workers_num >= 1)
        if workers_num == 1:
            self.worker(0, 0, file_size)
        else:
            pool = Pool(workers_num)
            for i in range(workers_num):
                start = i * file_size // workers_num
                end = (i+1) * file_size // workers_num
                pool.apply_async(func=self.worker, args=[i, start, end])
            pool.close()
            pool.join()

    def worker(self, proc_id, start, end):
        print("Worker %d is building dataset ... " % proc_id)
        set_seed(self.seed)
        docs_buffer = []
        document = []
        pos = start
        f_write = open(self.dataset_path + "-" + str(proc_id) + ".pt", "wb")
        # Open function in python3 does not support tell operation. We have to use codecs.
        with codecs.open(self.corpus_path, "r", "utf-8") as f:
            f.seek(start)
            instances = []
            while True:
                try:
                    line = f.readline()
                    src, tgt = line.strip().split()
                    src = [self.vocab.get(w) for w in self.tokenizer.tokenize(src)]
                    tgt = [self.vocab.get(w) for w in self.tokenizer.tokenize(tgt)]
                except:
                    continue

                seg = [1] * len(src)
                if len(src) >= self.seq_length:
                    src = src[:self.seq_length]
                    seg = seg[:self.seq_length]
                else:
                    while len(src) != self.seq_length:
                        src.append(PAD_ID)
                        seg.append(PAD_ID)

                if len(tgt) >= self.seq_length:
                    tgt = tgt[:self.seq_length]
                else:
                    while len(tgt) != self.seq_length:
                        tgt.append(PAD_ID)

                instances.append((src, tgt, seg))

                pos = f.tell()
                if pos >= end:
                    pickle.dump(instances, f_write)
                    break

        f_write.close()


class S2sDataLoader(object):
    """
    """
    def __init__(self, args, dataset_path, batch_size,  proc_id, proc_num, shuffle=True):
        self.batch_size = batch_size
        self.shuffle = shuffle
        self.proc_id = proc_id
        self.proc_num = proc_num

        self.f_read = open(dataset_path, "rb")
        self.start = 0
        self.end = 0
        self.buffer = []
        
    def _fill_buf(self):
        try:
            self.buffer = pickle.load(self.f_read)
        except EOFError:
            # Reach file end.
            self.f_read.seek(0)
            self.buffer = pickle.load(self.f_read)

        if self.shuffle:
            random.shuffle(self.buffer)
        self.start = 0
        self.end = len(self.buffer)

    def _empty(self):
        return self.start + self.batch_size*self.proc_num >= self.end

    def __del__(self):
        self.f_read.close()

    def __iter__(self):
        while True:
            if self._empty():
                self._fill_buf()
            instances = self.buffer[self.start + self.proc_id*self.batch_size: self.start + (self.proc_id+1)*self.batch_size]
            self.start += self.batch_size*self.proc_num
        
            src = []
            tgt = []
            seg = []

            for ins in instances:
                src.append(ins[0])
                tgt.append(ins[1])
                seg.append(ins[2])

            yield torch.LongTensor(src), \
                torch.LongTensor(tgt), \
                torch.LongTensor(seg)
