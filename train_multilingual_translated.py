import hydra
import wandb
import ctranslate2
import numpy as np
from tqdm.auto import tqdm
from omegaconf import OmegaConf, DictConfig

import torch
import torch.nn.functional as F
from datasets import load_dataset
from transformers import AutoTokenizer, AutoModel, get_scheduler

from sentence_embedder import SentenceEmbedderConfig, SentenceEmbedderModel


def get_batch(data, batch_size):
    en_sents, batch_idxs = [], []
    while len(en_sents) < batch_size:
        idx = np.random.randint(0, len(data))
        if idx in batch_idxs: continue

        item = data[idx]
        en_sents.append(item['en'])
        batch_idxs.append(idx)
    
    return en_sents


class Translator:
    def __init__(self, device):
        if 'cuda' in device:
            device, device_index = device.split(':')
            device_index = int(device_index)
        else:
            device = 'cpu'
            device_index = 0

        self.translator = ctranslate2.Translator("nllb-200-distilled-600M", device=device, device_index=device_index)
        self.tokenizer = AutoTokenizer.from_pretrained("facebook/nllb-200-distilled-600M")
        self.languages = list(self.tokenizer.lang_code_to_id.keys()) # NLLB가 제공하는 모든 언어를 학습에 사용할 경우
        
    def translate(self, sentences, tgt_lang):
        source = [self.tokenizer.tokenize(s, add_special_tokens=True) for s in sentences]
        target_prefix = [[tgt_lang]] * len(source)

        results = self.translator.translate_batch(source, target_prefix=target_prefix)
        results = [self.tokenizer.decode(self.tokenizer.convert_tokens_to_ids(res.hypotheses[0][1:])) for res in results]
        return results
    
    def translate_random_lang(self, sentences):
        source = [self.tokenizer.tokenize(s, add_special_tokens=True) for s in sentences]
        target_prefix = [[np.random.choice(self.languages)] for _ in range(len(source))]
        results = self.translator.translate_batch(source, target_prefix=target_prefix)
        results = [self.tokenizer.decode(self.tokenizer.convert_tokens_to_ids(res.hypotheses[0][1:])) for res in results]
        return results


def mean_pooling(token_embeddings, attention_mask):
    input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
    return torch.sum(token_embeddings * input_mask_expanded, 1) / torch.clamp(input_mask_expanded.sum(1), min=1e-9)


@hydra.main(config_path='.', config_name='cfg', version_base='1.2')
def main(cfg: DictConfig):
    print(OmegaConf.to_yaml(cfg))
    data = load_dataset('csv', data_dir=cfg.data_dir)
    data = data['train']
    en_sents = get_batch(data, cfg.batch_size)
    translator = Translator(cfg.device)

    teacher_tokenizer = AutoTokenizer.from_pretrained(cfg.teacher_name)
    teacher_model = AutoModel.from_pretrained(cfg.teacher_name)
    _ = teacher_model.eval().requires_grad_(False).to(cfg.device)

    student_tokenizer = AutoTokenizer.from_pretrained(cfg.student_name)
    student_config = SentenceEmbedderConfig(
        base_model_name = cfg.student_name,
        output_size = teacher_model.config.hidden_size,
        init_backbone = True
    )
    student_model = SentenceEmbedderModel(student_config)
    _ = student_model.train().to(cfg.device)

    student_tokenizer.save_pretrained(cfg.ckpt_dir)
    student_model.save_pretrained(cfg.ckpt_dir)

    optimizer = torch.optim.AdamW(student_model.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)
    scheduler = get_scheduler('cosine', optimizer, num_warmup_steps=cfg.num_warmup_steps, num_training_steps=cfg.num_training_steps)

    wandb.init(project='multilingual-sentence-embedder', config=cfg)
    pbar = tqdm(range(1, cfg.num_training_steps+1))
    for st in pbar:
        while True:
            try:
                en_sents = get_batch(data, cfg.batch_size)
                tr_sents = translator.translate_random_lang(en_sents)
                break
            except:
                continue
        
        teacher_en_inputs = teacher_tokenizer(en_sents, max_length=cfg.max_length, padding=True, truncation=True, return_tensors='pt').to(cfg.device)
        student_en_inputs = student_tokenizer(en_sents, max_length=cfg.max_length, padding=True, truncation=True, return_tensors='pt').to(cfg.device)
        student_tr_inputs = student_tokenizer(tr_sents, max_length=cfg.max_length, padding=True, truncation=True, return_tensors='pt').to(cfg.device)

        teacher_en_outputs = teacher_model(**teacher_en_inputs).last_hidden_state
        teacher_en_embeds = mean_pooling(teacher_en_outputs, teacher_en_inputs.attention_mask)

        student_en_outputs = student_model(**student_en_inputs).last_hidden_state
        student_en_embeds = mean_pooling(student_en_outputs, student_en_inputs.attention_mask)
        student_tr_outputs = student_model(**student_tr_inputs).last_hidden_state
        student_tr_embeds = mean_pooling(student_tr_outputs, student_tr_inputs.attention_mask)

        # word-level loss
        num_sents = len(en_sents)
        num_words = [len(s.split()) for s in en_sents]
        teacher_words_embeds, student_words_embeds = [], []
        for i in range(num_sents):
            num_sent_words = num_words[i]
            for j in range(num_sent_words):
                teacher_span = teacher_en_inputs.word_to_tokens(i, j)
                student_span = student_en_inputs.word_to_tokens(i, j)
                
                if teacher_span is not None and student_span is not None:
                    teacher_word_embeds = teacher_en_outputs[i, teacher_span.start:teacher_span.end].mean(dim=0)
                    teacher_words_embeds.append(teacher_word_embeds)
                    student_word_embeds = student_en_outputs[i, student_span.start:student_span.end].mean(dim=0)
                    student_words_embeds.append(student_word_embeds)

        if len(teacher_words_embeds) > 0 and len(student_words_embeds) > 0:
            teacher_words_embeds = torch.stack(teacher_words_embeds)
            student_words_embeds = torch.stack(student_words_embeds)
            word_loss = F.mse_loss(teacher_words_embeds, student_words_embeds)
        else:
            word_loss = 0.
        
        en_loss = F.mse_loss(teacher_en_embeds, student_en_embeds) 
        tr_loss = F.mse_loss(teacher_en_embeds, student_tr_embeds)
        loss = en_loss * 10. + tr_loss * 10. + word_loss
        
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        scheduler.step()

        log = {'loss': loss.item(), 'en': en_loss.item(), 'tr': tr_loss.item(), 'word': word_loss.item()}
        pbar.set_postfix(log)
        wandb.log(log)

        if st % 1000 == 0:
            student_tokenizer.save_pretrained(cfg.ckpt_dir)
            student_model.save_pretrained(cfg.ckpt_dir)

if __name__ == '__main__':
    main()
