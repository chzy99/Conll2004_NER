import argparse
import logging
import time
import os

import torch
from torch.optim import Adam
from torch.utils.data import TensorDataset
from torch.utils.data import DataLoader

from data_processor import DataProcessor
from modeling import BiLSTM


logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
logger = logging.getLogger(__name__)


def loss_fn(outputs, labels):
	"""
	:param outputs: (batch_sz * seq_len, num_of_tags)
	:param labels: (batch_sz, seq_len)
	"""
	labels = labels.view(-1)  # (batch_sz * seq_len)
	mask = (labels >= 0).float()
	num_of_tokens = int(torch.sum(mask).item())
	outputs = outputs[range(outputs.shape[0]), labels] * mask
	return -torch.sum(outputs) / num_of_tokens


def find_all_tags(predictions, dev_mask):
	tags = {
		"per": [],
		"loc": [],
		"org": []
	}
	idx = 0
	while idx < len(predictions):
		if dev_mask[idx] == 0 or predictions[idx] == 6:  # PAD or O
			idx += 1
			continue
		if predictions[idx] == 0:  # B-PER
			per_pos = idx
			length = 0  # number of I-PER
			seek = idx + 1
			while predictions[seek] == 1:  # I-PER
				length += 1
				seek += 1
			idx += (length + 1)
			tags["per"].append((per_pos, length))
		elif predictions[idx] == 2:  # B-ORG
			org_pos = idx
			length = 0
			seek = idx + 1
			while predictions[seek] == 3:
				length += 1
				seek += 1
			idx += (length + 1)
			tags["org"].append((org_pos, length))
		elif predictions[idx] == 4:  # B-LOC
			loc_pos = idx
			length = 0
			seek = idx + 1
			while predictions[seek] == 5:
				length += 1
				seek += 1
			idx += (length + 1)
			tags["loc"].append((loc_pos, length))
		else:
			idx += 1
	return tags


def precision(predictions, dev_labels, dev_mask):
	pre = []
	result = find_all_tags(predictions, dev_mask)
	for tag_name in result:
		for res in result[tag_name]:
			if (predictions[res[0]:res[0] + res[1] + 1] == dev_labels[res[0]:res[0] + res[1] + 1]).all():
				pre.append(1)
			else:
				pre.append(0)
	if len(pre) == 0:
		return 0
	return sum(pre) / len(pre)


def recall(predictions, dev_labels, dev_mask):
	rec = []
	result = find_all_tags(dev_labels, dev_mask)
	for tag_name in result:
		for res in result[tag_name]:
			if (predictions[res[0]:res[0] + res[1] + 1] == dev_labels[res[0]:res[0] + res[1] + 1]).all():
				rec.append(1)
			else:
				rec.append(0)
	if len(rec) == 0:
		return 0
	return sum(rec) / len(rec)


def compute_f1(predictions, dev_labels, dev_mask):
	predictions = predictions.cpu().numpy()
	predictions = predictions.astype("int32")
	dev_labels = dev_labels.cpu().numpy()
	dev_labels = dev_labels.astype("int32")
	dev_mask = dev_mask.cpu().numpy()
	dev_mask = dev_mask.astype("int32")
	p = precision(predictions, dev_labels, dev_mask)
	r = recall(predictions, dev_labels, dev_mask)
	if p == 0 and r == 0:
		return 0
	return (2 * p * r) / (p + r)


def evaluate(model, device, dev_labels, dev_dataloader):
	model.eval()
	all_predictions = torch.tensor([]).to(device)
	dev_batches = [batch for batch in dev_dataloader]
	for idx, (ids, _) in enumerate(dev_batches):
		input_ids = ids.to(device)
		with torch.no_grad():
			outputs = model(input_ids)
		predicted_lbl = torch.argmax(outputs, dim=1)  # make predictions
		all_predictions = torch.cat((all_predictions, predicted_lbl), dim=0)
	dev_labels = dev_labels.view(-1)
	dev_mask = (dev_labels >= 0).float()
	f1_score = compute_f1(all_predictions, dev_labels, dev_mask)
	return f1_score


def main(args):
	device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
	n_gpu = torch.cuda.device_count()

	data_processor = DataProcessor("./data/conll04_train.json", "./data/conll04_dev.json", "./data/conll04_test.json")
	(train_examples, dev_examples), vocabulary = data_processor.get_conll_examples(do_training=True)
	logger.info("Example format test")
	logger.info("Orig id: %d" % train_examples[0].orig_id)
	logger.info("Tokens: %s" % (" ".join(train_examples[0].tokens)))
	logger.info("Label: %s" % (" ".join(train_examples[0].label)))

	train_features, train_label = data_processor.convert_example_to_features(train_examples, vocabulary)
	dev_features, dev_label = data_processor.convert_example_to_features(dev_examples, vocabulary)
	weight_matrix = data_processor.build_lookup_matrix(vocabulary)

	train_data = TensorDataset(train_features, train_label)
	train_dataloader = DataLoader(train_data, batch_size=args.batch_size, shuffle=True)  # shuffle
	train_batches = [batch for batch in train_dataloader]

	dev_data = TensorDataset(dev_features, dev_label)
	dev_dataloader = DataLoader(dev_data, batch_size=args.batch_size)

	eval_step = max(1, len(train_batches) // 5)

	model = BiLSTM(weight_matrix, 20, 7)
	model.to(device)
	if n_gpu > 1:
		model = torch.nn.DataParallel(model)

	optimizer = Adam(model.parameters(), lr=0.7, eps=0.1)

	tr_loss = 0
	tr_num_steps = 0
	max_score = 0.0
	start_time = time.time()
	for epoch in range(args.num_train_epochs):
		model.train()
		logger.info("Start epoch #{} (lr = {})...".format(epoch, 0.01))
		for step, batch in enumerate(train_batches):
			batch = tuple(t.to(device) for t in batch)
			input_ids, input_label = batch

			outputs = model(input_ids)
			loss = loss_fn(outputs, input_label)
			if n_gpu > 1:
				loss = loss.mean()
			tr_loss += loss.item()
			tr_num_steps += 1

			loss.backward()
			optimizer.step()
			optimizer.zero_grad()

			if (step + 1) % eval_step == 0:
				logger.info('Epoch: {}, Step: {} / {}, used_time = {:.2f}s, loss = {:.6f}'.format(
					epoch, step + 1, len(train_batches), time.time() - start_time, tr_loss / tr_num_steps))
				save_model = False
				if True:  # do_eval
					score = evaluate(model, device, dev_label, dev_dataloader)
					model.train()
					if score > max_score:
						max_score = score
						save_model = True
						logger.info("!!! Best dev %s (lr=%s, epoch=%d): %.6f" % ("F1", str(0.01), epoch, score))
				else:
					save_model = True
				if save_model:
					model_to_save = model.module if hasattr(model, 'module') else model
					output_model_file = os.path.join(args.output_dir, "pytorch_model.bin")
					torch.save(model_to_save.state_dict(), output_model_file)
					if max_score:
						with open(os.path.join(args.output_dir, "eval_results.txt"), "w") as writer:
							writer.write("Best eval result: F1 = %.4f" % max_score)
	# if True:
	# 	[test_examples], _ = data_processor.get_conll_examples(do_training=False)
	# 	test_features, test_label = data_processor.convert_example_to_features(test_examples, vocabulary)
	# 	test_data = TensorDataset(test_features, test_label)
	# 	test_dataloader = DataLoader(test_data, batch_size=args.batch_size)
	#
	# 	model = BiLSTM(weight_matrix, 20, 7)
	# 	model.load_state_dict(torch.load(os.path.join(args.output_dir, "pytorch_model.bin")))
	#
	# 	eval_result_file = os.path.join(args.output_dir, "eval_results.txt")
	# 	if os.path.isfile(eval_result_file):
	# 		with open(eval_result_file) as f:
	# 			line = f.readline()
	# 		logger.info(line)
	# 		f.close()
	#
	# 	test_score = evaluate(model, device, test_label, test_dataloader)
	# 	result = "test result: F1 = %.6f" % test_score
	# 	logger.info(result)


if __name__ == "__main__":
	parser = argparse.ArgumentParser()
	# required parameters
	parser.add_argument(
		"--train_file",
		default="./data/conll04_train.json",
		type=str,
		help="The input training file. If a data dir is specified, will look for the file there",
	)
	parser.add_argument(
		"--dev_file",
		default="./data/conll04_dev.json",
		type=str,
		help="The input evaluation file. If a data dir is specified, will look for the file there",
	)
	parser.add_argument(
		"--test_file",
		default="./data/conll04_test.json",
		type=str,
		help="The input evaluation file. If a data dir is specified, will look for the file there",
	)
	parser.add_argument(
		"--output_dir",
		default="./output/",
		type=str,
	)
	parser.add_argument("--do_train", action="store_true", help="Whether to run training.")
	parser.add_argument("--do_eval", action="store_true", help="Whether to run eval on the dev set.")
	parser.add_argument("--batch_size", default=8, type=int, help="Batch size for training.")
	parser.add_argument("--adam_epsilon", default=1e-8, type=float, help="Epsilon for Adam optimizer.")
	parser.add_argument(
		"--num_train_epochs", default=50, type=float, help="Total number of training epochs to perform."
	)
	parser.add_argument("--seed", type=int, default=42, help="random seed for initialization")
	args = parser.parse_args()
	if not os.path.exists(args.output_dir):
		os.makedirs(args.output_dir)

	main(args)