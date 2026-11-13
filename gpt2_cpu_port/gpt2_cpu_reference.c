/* CPU reference for GPT-2 prefill, replacing the uploaded PIM timing hooks.
 * FP32, batch=1, causal attention, GELU-new, tied token embedding / LM head.
 * Checkpoint format and limitations are documented in README.md.
 * This is scalar CPU execution: it does not emulate GDDR6-AiM compute.
 */
#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef GPT2_USE_M5
#include <gem5/m5ops.h>
#endif

typedef struct {
    float *ln1_g, *ln1_b, *qkv_w, *qkv_b, *attn_w, *attn_b;
    float *ln2_g, *ln2_b, *fc_w, *fc_b, *proj_w, *proj_b;
} Block;
typedef struct {
    uint32_t vocab, context, dim, layers, heads, inner;
    float eps, *storage, *wte, *wpe, *lnf_g, *lnf_b;
    Block *block;
} Model;
typedef struct {
    float *x, *norm, *qkv, *attn, *proj, *ff, *ffout, *scores, *logits;
} Workspace;

static void die(const char *message) {
    fprintf(stderr, "error: %s\n", message);
    exit(EXIT_FAILURE);
}
static void *alloc_bytes(size_t bytes) {
    void *p = malloc(bytes);
    if (!p) die("allocation failed");
    return p;
}
static float *alloc_floats(size_t count) {
    if (count > SIZE_MAX / sizeof(float)) die("allocation size overflow");
    return alloc_bytes(count * sizeof(float));
}
static uint32_t read_u32(FILE *f) {
    unsigned char b[4];
    if (fread(b, 1, 4, f) != 4) die("truncated checkpoint header");
    return (uint32_t)b[0] | (uint32_t)b[1] << 8 |
           (uint32_t)b[2] << 16 | (uint32_t)b[3] << 24;
}
static float *take(float **cursor, size_t count) {
    float *out = *cursor;
    *cursor += count;
    return out;
}
static size_t parameter_count(const Model *m) {
    const size_t d = m->dim, f = m->inner;
    return (size_t)m->vocab*d + (size_t)m->context*d + 2*d +
           (size_t)m->layers * (4*d*d + 2*d*f + 9*d + f);
}
static Model load_model(const char *path) {
    const uint16_t endian = 1;
    if (*(const unsigned char *)&endian != 1 || sizeof(float) != 4)
        die("this FP32 checkpoint loader requires a little-endian host");
    FILE *f = fopen(path, "rb");
    if (!f) die("cannot open checkpoint (use --model with an absolute path)");
    char magic[8];
    if (fread(magic, 1, 8, f) != 8 || memcmp(magic, "G2CPU001", 8))
        die("unsupported checkpoint: export with export_hf_gpt2.py first");
    if (read_u32(f) != 1) die("unsupported checkpoint version");
    Model m = {0};
    m.vocab=read_u32(f); m.context=read_u32(f); m.dim=read_u32(f);
    m.layers=read_u32(f); m.heads=read_u32(f); m.inner=read_u32(f);
    if (read_u32(f) != 0) die("unsupported checkpoint flags");
    if (fread(&m.eps, sizeof(float), 1, f) != 1) die("missing layernorm epsilon");
    if (!m.vocab || m.vocab>1000000 || !m.context || m.context>4096 ||
        !m.dim || m.dim>4096 || !m.layers || m.layers>48 ||
        !m.heads || m.heads>128 || m.dim%m.heads || !m.inner ||
        m.inner>32768 || !isfinite(m.eps) || m.eps<=0)
        die("invalid or unsupported model dimensions");
    size_t n = parameter_count(&m);
    m.storage = alloc_floats(n);
    if (fread(m.storage, sizeof(float), n, f) != n) die("truncated tensor data");
    if (fgetc(f) != EOF || ferror(f)) die("checkpoint has trailing data or read error");
    fclose(f);
    float *p=m.storage;
    const size_t d=m.dim, inner=m.inner;
    m.wte=take(&p,(size_t)m.vocab*d); m.wpe=take(&p,(size_t)m.context*d);
    m.lnf_g=take(&p,d); m.lnf_b=take(&p,d);
    m.block=alloc_bytes(m.layers*sizeof(Block));
    for (uint32_t l=0;l<m.layers;l++) {
        Block *b=&m.block[l];
        b->ln1_g=take(&p,d); b->ln1_b=take(&p,d);
        b->qkv_w=take(&p,3*d*d); b->qkv_b=take(&p,3*d);
        b->attn_w=take(&p,d*d); b->attn_b=take(&p,d);
        b->ln2_g=take(&p,d); b->ln2_b=take(&p,d);
        b->fc_w=take(&p,inner*d); b->fc_b=take(&p,inner);
        b->proj_w=take(&p,d*inner); b->proj_b=take(&p,d);
    }
    if ((size_t)(p-m.storage)!=n) die("internal checkpoint layout error");
    return m;
}
static size_t load_tokens(const char *path, int *ids, const Model *m) {
    FILE *f=fopen(path,"r");
    if (!f) die("cannot open token file");
    long id;
    size_t count=0;
    int status;
    while ((status=fscanf(f,"%ld",&id))==1) {
        if (count>=m->context || id<0 || (unsigned long)id>=m->vocab)
            die("token ID or sequence length is outside model limits");
        ids[count++]=(int)id;
    }
    if (status!=EOF || ferror(f)) die("token file must contain whitespace-separated integers");
    fclose(f);
    if (!count) die("token file is empty");
    return count;
}
static Workspace make_workspace(size_t seq, const Model *m) {
    Workspace w;
    size_t sd=seq*m->dim;
    w.x=alloc_floats(sd); w.norm=alloc_floats(sd);
    w.qkv=alloc_floats(3*sd); w.attn=alloc_floats(sd);
    w.proj=alloc_floats(sd); w.ff=alloc_floats(seq*m->inner);
    w.ffout=alloc_floats(sd); w.scores=alloc_floats(seq);
    w.logits=alloc_floats(m->vocab);
    return w;
}
static void layernorm(const float *in, float *out, const float *g,
                      const float *b, size_t rows, size_t dim, float eps) {
    for (size_t t=0;t<rows;t++) {
        double mean=0.0, variance=0.0;
        for (size_t i=0;i<dim;i++) mean+=in[t*dim+i];
        mean/=dim;
        for (size_t i=0;i<dim;i++) {
            double delta=in[t*dim+i]-mean;
            variance+=delta*delta;
        }
        float inv=1.0f/sqrtf((float)(variance/dim)+eps);
        for (size_t i=0;i<dim;i++)
            out[t*dim+i]=((in[t*dim+i]-(float)mean)*inv)*g[i]+b[i];
    }
}
/* Exported linear weights are output-major: weight[out_features][in_features]. */
static void linear(const float *in, float *out, const float *weight,
                   const float *bias, size_t rows, size_t nin, size_t nout) {
    for (size_t t=0;t<rows;t++) {
        for (size_t o=0;o<nout;o++) {
            float sum=bias?bias[o]:0.0f;
            for (size_t i=0;i<nin;i++) sum+=in[t*nin+i]*weight[o*nin+i];
            out[t*nout+o]=sum;
        }
    }
}
static void attention(const float *qkv, float *out, float *scores,
                      size_t seq, size_t dim, size_t heads) {
    size_t hd=dim/heads;
    float scale=1.0f/sqrtf((float)hd);
    for (size_t t=0;t<seq;t++) for (size_t h=0;h<heads;h++) {
        float max=-INFINITY;
        for (size_t j=0;j<=t;j++) {
            float sum=0.0f;
            for (size_t i=0;i<hd;i++)
                sum+=qkv[t*3*dim+h*hd+i]*qkv[j*3*dim+dim+h*hd+i];
            scores[j]=sum*scale;
            if (scores[j]>max) max=scores[j];
        }
        float denominator=0.0f;
        for (size_t j=0;j<=t;j++) {
            scores[j]=expf(scores[j]-max);
            denominator+=scores[j];
        }
        for (size_t i=0;i<hd;i++) {
            float sum=0.0f;
            for (size_t j=0;j<=t;j++)
                sum+=(scores[j]/denominator)*qkv[j*3*dim+2*dim+h*hd+i];
            out[t*dim+h*hd+i]=sum;
        }
    }
}
static void gelu_new(float *x, size_t count) {
    for (size_t i=0;i<count;i++) {
        float a=x[i];
        x[i]=0.5f*a*(1.0f+tanhf(0.7978845608028654f*(a+0.044715f*a*a*a)));
    }
}
static void forward(const Model *m, Workspace *w, const int *ids, size_t seq,
                    size_t blocks, int hidden_mode) {
    size_t d=m->dim, sd=seq*d;
    for (size_t t=0;t<seq;t++) for (size_t i=0;i<d;i++)
        w->x[t*d+i]=m->wte[(size_t)ids[t]*d+i]+m->wpe[t*d+i];
    for (size_t l=0;l<blocks;l++) {
        const Block *b=&m->block[l];
        layernorm(w->x,w->norm,b->ln1_g,b->ln1_b,seq,d,m->eps);
        linear(w->norm,w->qkv,b->qkv_w,b->qkv_b,seq,d,3*d);
        attention(w->qkv,w->attn,w->scores,seq,d,m->heads);
        linear(w->attn,w->proj,b->attn_w,b->attn_b,seq,d,d);
        for (size_t i=0;i<sd;i++) w->x[i]+=w->proj[i];
        layernorm(w->x,w->norm,b->ln2_g,b->ln2_b,seq,d,m->eps);
        linear(w->norm,w->ff,b->fc_w,b->fc_b,seq,d,m->inner);
        gelu_new(w->ff,seq*m->inner);
        linear(w->ff,w->ffout,b->proj_w,b->proj_b,seq,m->inner,d);
        for (size_t i=0;i<sd;i++) w->x[i]+=w->ffout[i];
    }
    if (!hidden_mode) {
        layernorm(w->x,w->norm,m->lnf_g,m->lnf_b,seq,d,m->eps);
        linear(w->norm+(seq-1)*d,w->logits,m->wte,NULL,1,d,m->vocab);
    }
}
static int parse_positive(const char *text) {
    char *end;
    errno=0;
    long n=strtol(text,&end,10);
    if (errno || end==text || *end || n<1 || n>100000) die("invalid positive integer argument");
    return (int)n;
}
static void usage(void) {
    puts("gpt2_cpu --model MODEL.bin --tokens tokens.txt [--mode logits|hidden]\n"
         "         [--blocks N] [--repeat N] [--output result.f32] [--roi]\n"
         "hidden: post-block states before final LN; partial blocks are diagnostic.\n"
         "logits: full model, last input token only, no KV cache or sampling.");
}
int main(int argc, char **argv) {
    const char *model_path=NULL,*token_path=NULL,*output_path=NULL;
    int hidden_mode=0,blocks_arg=0,repeats=1,roi=0;
    for (int i=1;i<argc;i++) {
        if (!strcmp(argv[i],"--help")) {usage();return 0;}
        if (!strcmp(argv[i],"--roi")) {roi=1;continue;}
        if (i+1>=argc) die("missing option value");
        const char *key=argv[i],*value=argv[++i];
        if (!strcmp(key,"--model")) model_path=value;
        else if (!strcmp(key,"--tokens")) token_path=value;
        else if (!strcmp(key,"--output")) output_path=value;
        else if (!strcmp(key,"--blocks")) blocks_arg=parse_positive(value);
        else if (!strcmp(key,"--repeat")) repeats=parse_positive(value);
        else if (!strcmp(key,"--mode")) {
            if (!strcmp(value,"hidden")) hidden_mode=1;
            else if (!strcmp(value,"logits")) hidden_mode=0;
            else die("--mode must be logits or hidden");
        } else die("unknown option");
    }
    if (!model_path || !token_path) {usage();return 2;}
#ifndef GPT2_USE_M5
    if (roi) die("--roi requires a build with WITH_M5=1");
#endif
    Model m=load_model(model_path);
    size_t blocks=blocks_arg?(size_t)blocks_arg:m.layers;
    if (blocks>m.layers || (!hidden_mode && blocks!=m.layers))
        die("partial blocks require --mode hidden and cannot exceed model layers");
    int *ids=alloc_bytes(m.context*sizeof(int));
    size_t seq=load_tokens(token_path,ids,&m);
    Workspace w=make_workspace(seq,&m);
    printf("GPT-2 CPU reference: D=%u L=%u H=%u F=%u V=%u tokens=%zu\n",
           m.dim,m.layers,m.heads,m.inner,m.vocab,seq);
    printf("mode=%s blocks=%zu/%u repeats=%d weights=%zu bytes ROI=%s\n",
           hidden_mode?"hidden-before-final-LN":"last-token-logits",blocks,m.layers,
           repeats,parameter_count(&m)*sizeof(float),roi?"on":"off");
    fflush(stdout);
    volatile float consume=0.0f;
#ifdef GPT2_USE_M5
    if (roi) m5_work_begin(0,0);
#endif
    for (int r=0;r<repeats;r++) {
        forward(&m,&w,ids,seq,blocks,hidden_mode);
        consume+=hidden_mode?w.x[0]:w.logits[0];
    }
#ifdef GPT2_USE_M5
    if (roi) m5_work_end(0,0);
#endif
    (void)consume;
    const float *result=hidden_mode?w.x:w.logits;
    size_t count=hidden_mode?seq*m.dim:m.vocab;
    double checksum=0.0;
    size_t argmax=0;
    for (size_t i=0;i<count;i++) {
        if (!isfinite(result[i])) die("non-finite output");
        checksum+=result[i];
        if (result[i]>result[argmax]) argmax=i;
    }
    printf("output_elements=%zu checksum=%.9g",count,checksum);
    if (!hidden_mode) printf(" next_token_id=%zu",argmax);
    puts("");
    if (output_path) {
        FILE *out=fopen(output_path,"wb");
        if (!out) die("cannot create output file");
        if (fwrite(result,sizeof(float),count,out)!=count || fclose(out)) die("output write failed");
    }
    free(ids); free(m.block); free(m.storage);
    free(w.x);free(w.norm);free(w.qkv);free(w.attn);free(w.proj);
    free(w.ff);free(w.ffout);free(w.scores);free(w.logits);
    return 0;
}
