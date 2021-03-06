#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
 SELFIES: a robust representation of semantically constrained graphs with an
           example application in chemistry (https://arxiv.org/abs/1905.13741)
           by Mario Krenn, Florian Haese, AkshatKuman Nigam, Pascal Friederich, Alan Aspuru-Guzik


           Variational Auto Encoder (VAE) for chemistry
                  comparing SMILES and SELFIES representation using reconstruction
                  quality, diversity and latent space validity as metrics of
                  interest
                  v0.1.0 -- 04. August 2019

 information:
     ML framework: pytorch
     chemistry framework: RDKit


     settings.yml
             contains link to data file containing SMILES encoded molecule, and
             hyperparameters of neural network model and training

     get_selfie_and_smiles_encodings_for_dataset
             generate complete encoding (inclusive alphabet) for SMILES and SELFIES given a data file

     VAE_encode
             fully connection, 3 layer neural network - encodes a one-hot representation
             of molecule (in SMILES or SELFIES representation) to latent space

     VAE_decode
             decodes point in latent space using an RNN

     latent_space_quality
             samples points from latent space, decodes them into molecules,
             calculates chemical validity (using RDKit's MolFromSmiles), calculates
             diversity

     environment.yml
             shows dependencies
             Particularily important: RDKit and SELFIES (via 'pip install selfies')


 tested at:
     - Python 3.7.1
     - Python 3.6.8

     CPU and GPU supported




Note: semantic validity is only implemented so far for atoms described in
      Table 2 of our paper. This corresponds to (non-ionic) QM9. Other chemical
      constraints might generate additional mistakes. Syntactical constraints
      are always fulfilled
      - Aromatic Symbols: they have additional semantic constraints, thus to reduce
                          invalidity due to aromatic constraints, one can
                          de-aromatize molecules (aromatic symbols are simplifications
                          in SMILES). Otherwise, one could add the semantic constraints
                          (this could be done in an automated way, but is not implemented yet)


For comments, bug reports or feature ideas, please send an email to
mario.krenn@utoronto.ca and alan@aspuru.com

"""
import os, sys, time
import numpy as np
import torch
import pandas as pd
import selfies
import yaml
from torch import nn
from random import shuffle

sys.path.append('VAE_dependencies')
from data_loader import multiple_smile_to_hot, multiple_selfies_to_hot, len_selfie, split_selfie
from rdkit.Chem import MolFromSmiles
from rdkit import rdBase
rdBase.DisableLog('rdApp.error')


def _make_dir(directory):
    os.makedirs(directory)

def save_models(encoder, decoder, epoch):
    out_dir = './saved_models/{}'.format(epoch)
    _make_dir(out_dir)
    torch.save(encoder, '{}/E'.format(out_dir))
    torch.save(decoder, '{}/D'.format(out_dir))



class VAE_encode(nn.Module):

    def __init__(self, layer_1d, layer_2d, layer_3d, latent_dimension):
        """
        Fully Connected layers to encode molecule to latent space
        """
        super(VAE_encode, self).__init__()

        # Reduce dimension upto second last layer of Encoder
        self.encode_nn = nn.Sequential(
            nn.Linear(len_max_molec1Hot, layer_1d),
            nn.ReLU(),
            nn.Linear(layer_1d, layer_2d),
            nn.ReLU(),
            nn.Linear(layer_2d, layer_3d),
			nn.ReLU()
        )

        # Latent space mean
        self.encode_mu = nn.Linear(layer_3d, latent_dimension)

        # Latent space variance
        self.encode_log_var = nn.Linear(layer_3d, latent_dimension)


    def reparameterize(self, mu, log_var):
        """
        This trick is explained well here:
            https://stats.stackexchange.com/a/16338
        """
        std = torch.exp(0.5 * log_var)
        eps = torch.randn_like(std)
        return eps.mul(std).add_(mu)


    def forward(self, x):
        """
        Pass throught the Encoder
        """
        # Get results of encoder network
        h1 = self.encode_nn(x)

        # latent space
        mu = self.encode_mu(h1)
        log_var = self.encode_log_var(h1)

        # Reparameterize
        z = self.reparameterize(mu, log_var)
        return z, mu, log_var



class VAE_decode(nn.Module):

    def __init__(self, latent_dimension, gru_stack_size, gru_neurons_num):
        """
        Through Decoder
        """
        super(VAE_decode, self).__init__()
        self.gru_stack_size = gru_stack_size
        self.gru_neurons_num = gru_neurons_num

        # Simple Decoder
        self.decode_RNN  = nn.GRU(
                input_size  = latent_dimension,
                hidden_size = gru_neurons_num,
                num_layers  = gru_stack_size,
                batch_first = False)

        self.decode_FC = nn.Sequential(
            nn.Linear(gru_neurons_num, len_alphabet),
        )


    def init_hidden(self, batch_size = 1):
        weight = next(self.parameters())
        return weight.new_zeros(self.gru_stack_size, batch_size, self.gru_neurons_num)


    def forward(self, z, hidden):
        """
        A forward pass throught the entire model.
        """
        # Decode
        l1, hidden = self.decode_RNN(z, hidden)
        decoded = self.decode_FC(l1)        # fully connected layer

        return decoded, hidden



def is_correct_smiles(smiles):
    """
    Using RDKit to calculate whether molecule is syntactically and semantically valid.
    """
    if smiles == "":
        return 0

    try:
        return int(MolFromSmiles(smiles, sanitize=True) is not None)
    except Exception:
        return 0


def sample_latent_space(latent_dimension):
    model_encode.eval()
    model_decode.eval()

    fancy_latent_point=torch.normal(torch.zeros(latent_dimension),torch.ones(latent_dimension))

    hidden = model_decode.init_hidden()
    gathered_atoms = []
    for ii in range(len_max_molec):                 # runs over letters from molecules (len=size of largest molecule)
        fancy_latent_point = fancy_latent_point.reshape(1, 1, latent_dimension)
        fancy_latent_point=fancy_latent_point.to(device)
        decoded_one_hot, hidden = model_decode(fancy_latent_point, hidden)

        decoded_one_hot = decoded_one_hot.flatten()
        decoded_one_hot = decoded_one_hot.detach()

        soft = nn.Softmax(0)
        decoded_one_hot = soft(decoded_one_hot)

        _,max_index=decoded_one_hot.max(0)
        gathered_atoms.append(max_index.data.cpu().numpy().tolist())

    model_encode.train()
    model_decode.train()

    return gathered_atoms



def latent_space_quality(latent_dimension, encoding_alphabet, sample_num):
    total_correct = 0
    all_correct_molecules = set()
    print(f"latent_space_quality:"
          f" Take {sample_num} samples from the latent space")

    for sample_i in range(1, sample_num + 1):

        molecule_pre = ''
        for ii in sample_latent_space(latent_dimension):
            molecule_pre += encoding_alphabet[ii]
        molecule = molecule_pre.replace(' ', '')

        if type_of_encoding == 1:  # if SELFIES, decode to SMILES
            molecule = selfies.decoder(molecule)

        if is_correct_smiles(molecule):
            total_correct += 1
            all_correct_molecules.add(molecule)

    return total_correct, len(all_correct_molecules)


def quality_in_validation_set(data_valid):
    x = [i for i in range(len(data_valid))]  # random shuffle input
    shuffle(x)
    data_valid = data_valid[x]

    quality_list=[]
    for batch_iteration in range(min(25,num_batches_valid)):  # batch iterator

        current_smiles_start, current_smiles_stop = batch_iteration * batch_size, (batch_iteration + 1) * batch_size
        inp_smile_hot = data_valid[current_smiles_start : current_smiles_stop]

        inp_smile_encode = inp_smile_hot.reshape(inp_smile_hot.shape[0], inp_smile_hot.shape[1] * inp_smile_hot.shape[2])
        latent_points, mus, log_vars = model_encode(inp_smile_encode)
        latent_points = latent_points.reshape(1, batch_size, latent_points.shape[1])

        hidden = model_decode.init_hidden(batch_size = batch_size)
        decoded_one_hot = torch.zeros(batch_size, inp_smile_hot.shape[1], inp_smile_hot.shape[2]).to(device)
        for seq_index in range(inp_smile_hot.shape[1]):
            decoded_one_hot_line, hidden  = model_decode(latent_points, hidden)
            decoded_one_hot[:, seq_index, :] = decoded_one_hot_line[0]

        decoded_one_hot = decoded_one_hot.reshape(batch_size * inp_smile_hot.shape[1], inp_smile_hot.shape[2])
        _, label_atoms  = inp_smile_hot.max(2)
        label_atoms     = label_atoms.reshape(batch_size * inp_smile_hot.shape[1])

        # assess reconstruction quality
        _, decoded_max_indices = decoded_one_hot.max(1)
        _, input_max_indices   = inp_smile_hot.reshape(batch_size * inp_smile_hot.shape[1], inp_smile_hot.shape[2]).max(1)

        differences = 1. - torch.abs(decoded_max_indices - input_max_indices)
        differences = torch.clamp(differences, min = 0., max = 1.).double()
        quality     = 100. * torch.mean(differences)
        quality     = quality.detach().cpu().numpy()
        quality_list.append(quality)

    return(np.mean(quality_list))






def train_model(data_train, data_valid, num_epochs, latent_dimension, lr_enc, lr_dec, KLD_alpha, sample_num, encoding_alphabet):
    """
    Train the Variational Auto-Encoder
    """

    print('num_epochs: ',num_epochs)

    # initialize an instance of the model
    optimizer_encoder = torch.optim.Adam(model_encode.parameters(), lr=lr_enc)
    optimizer_decoder = torch.optim.Adam(model_decode.parameters(), lr=lr_dec)

    data_train = data_train.clone().detach()
    data_train=data_train.to(device)

    #print(data)
    quality_valid_list=[0,0,0,0];
    for epoch in range(num_epochs):
        x = [i for i in range(len(data_train))]  # random shuffle input
        shuffle(x)

        data_train  = data_train[x]
        start = time.time()
        for batch_iteration in range(num_batches_train):  # batch iterator

            loss, recon_loss, kld = 0., 0., 0.

            # manual batch iterations
            current_smiles_start, current_smiles_stop = batch_iteration * batch_size, (batch_iteration + 1) * batch_size
            inp_smile_hot = data_train[current_smiles_start : current_smiles_stop]

            # reshaping for efficient parallelization
            inp_smile_encode = inp_smile_hot.reshape(inp_smile_hot.shape[0], inp_smile_hot.shape[1] * inp_smile_hot.shape[2])
            latent_points, mus, log_vars = model_encode(inp_smile_encode)
            latent_points = latent_points.reshape(1, batch_size, latent_points.shape[1])

            # standard Kullback–Leibler divergence
            kld += -0.5 * torch.mean(1. + log_vars - mus.pow(2) - log_vars.exp())

            # initialization hidden internal state of RNN (RNN has two inputs and two outputs:)
            #    input: latent space & hidden state
            #    output: onehot encoding of one character of molecule & hidden state
            #    the hidden state acts as the internal memory
            hidden = model_decode.init_hidden(batch_size = batch_size)

            # decoding from RNN N times, where N is the length of the largest molecule (all molecules are padded)
            decoded_one_hot = torch.zeros(batch_size, inp_smile_hot.shape[1], inp_smile_hot.shape[2]).to(device)
            for seq_index in range(inp_smile_hot.shape[1]):
                decoded_one_hot_line, hidden  = model_decode(latent_points, hidden)
                decoded_one_hot[:, seq_index, :] = decoded_one_hot_line[0]


            decoded_one_hot = decoded_one_hot.reshape(batch_size * inp_smile_hot.shape[1], inp_smile_hot.shape[2])
            _, label_atoms  = inp_smile_hot.max(2)
            label_atoms     = label_atoms.reshape(batch_size * inp_smile_hot.shape[1])

            # we use cross entropy of expected symbols and decoded one-hot
            criterion   = torch.nn.CrossEntropyLoss()
            recon_loss += criterion(decoded_one_hot, label_atoms)

            loss += recon_loss + KLD_alpha * kld

            # perform back propogation
            optimizer_encoder.zero_grad()
            optimizer_decoder.zero_grad()
            loss.backward(retain_graph=True)
            nn.utils.clip_grad_norm_(model_decode.parameters(), 0.5)
            optimizer_encoder.step()
            optimizer_decoder.step()

            if batch_iteration % 30 == 0:
                end = time.time()

                # assess reconstruction quality
                _, decoded_max_indices = decoded_one_hot.max(1)
                _, input_max_indices   = inp_smile_hot.reshape(batch_size * inp_smile_hot.shape[1], inp_smile_hot.shape[2]).max(1)

                differences = 1. - torch.abs(decoded_max_indices - input_max_indices)
                differences = torch.clamp(differences, min = 0., max = 1.).double()
                quality     = 100. * torch.mean(differences)
                quality     = quality.detach().cpu().numpy()

                qualityValid=quality_in_validation_set(data_valid)

                new_line = 'Epoch: %d,  Batch: %d / %d,\t(loss: %.4f\t| quality: %.4f | quality_valid: %.4f)\tELAPSED TIME: %.5f' % (epoch, batch_iteration, num_batches_train, loss.item(), quality, qualityValid, end - start)
                print(new_line)
                start = time.time()



        qualityValid = quality_in_validation_set(data_valid)
        quality_valid_list.append(qualityValid)

        # only measure validity of reconstruction improved
        quality_increase = len(quality_valid_list) - np.argmax(quality_valid_list)
        if quality_increase == 1 and quality_valid_list[-1] > 50.:
            corr, unique = latent_space_quality(latent_dimension,sample_num = sample_num, encoding_alphabet=encoding_alphabet)
        else:
            corr, unique = -1., -1.

        new_line = 'Validity: %.5f %% | Diversity: %.5f %% | Reconstruction: %.5f %%' % (corr * 100. / sample_num, unique * 100. / sample_num, qualityValid)

        print(new_line)
        with open('results.dat', 'a') as content:
            content.write(new_line + '\n')

        if quality_valid_list[-1] < 70. and epoch > 200:
            break

        if quality_increase > 20:
            print('Early stopping criteria')
            break


def get_selfie_and_smiles_encodings_for_dataset(filename_data_set_file_smiles):
    """
    Returns encoding, alphabet and length of largest molecule in SMILES and SELFIES, given a file containing SMILES molecules.
    input:
        csv file with molecules. Column's name must be 'smiles'.
    output:
        - selfies encoding
        - selfies alphabet
        - longest selfies string
        - smiles encoding (equivalent to file content)
        - smiles alphabet (character based)
        - longest smiles string
    """

    df = pd.read_csv(filename_data_set_file_smiles)

    smiles_list = np.asanyarray(df.smiles)
    smiles_alphabet = list(set(''.join(smiles_list)))
    smiles_alphabet.append(' ')  # for padding
    largest_smiles_len = len(max(smiles_list, key=len))

    print('--> Translating SMILES to SELFIES...')
    selfies_list = list(map(selfies.encoder, smiles_list))
    largest_selfies_len = max(len_selfie(s) for s in selfies_list)

    all_selfies_chars = split_selfie(''.join(selfies_list))
    all_selfies_chars.append('[epsilon]')
    selfies_alphabet = list(set(all_selfies_chars))

    print('Finished translating SMILES to SELFIES.')

    return(selfies_list, selfies_alphabet, largest_selfies_len, smiles_list, smiles_alphabet, largest_smiles_len)


if __name__ == '__main__':
    try:
        content = open('logfile.dat', 'w')
        content.close()
        content = open('results.dat', 'w')
        content.close()

        if os.path.exists("settings.yml"):
            user_settings=yaml.safe_load(open("settings.yml","r"))
            settings = user_settings
        else:
            print("Expected a file settings.yml but didn't find it.")
            print()
            exit()


        print('--> Acquiring data...')
        type_of_encoding = settings['data']['type_of_encoding']
        file_name_smiles = settings['data']['smiles_file']

        selfies_list, selfies_alphabet, largest_selfies_len, smiles_list, smiles_alphabet, largest_smiles_len=get_selfie_and_smiles_encodings_for_dataset(file_name_smiles)

        print('Finished acquiring data.')

        if type_of_encoding == 0:
            print('Representation: SMILES')
            encoding_alphabet=smiles_alphabet
            encoding_list=smiles_list
            largest_molecule_len = largest_smiles_len
            print('--> Creating one-hot encoding...')
            data = multiple_smile_to_hot(smiles_list, largest_molecule_len, encoding_alphabet)
            print('Finished creating one-hot encoding.')
        elif type_of_encoding == 1:
            print('Representation: SELFIES')

            encoding_alphabet=selfies_alphabet
            encoding_list=selfies_list
            largest_molecule_len=largest_selfies_len

            print('--> Creating one-hot encoding...')
            data = multiple_selfies_to_hot(encoding_list, largest_molecule_len, encoding_alphabet)
            print('Finished creating one-hot encoding.')

        len_max_molec = data.shape[1]
        len_alphabet = data.shape[2]
        len_max_molec1Hot = len_max_molec * len_alphabet
        print(' ')
        print('Alphabet has ', len_alphabet, ' letters, largest molecule is ', len_max_molec, ' letters.')

        data_parameters = settings['data']
        batch_size = data_parameters['batch_size']

        encoder_parameter = settings['encoder']
        decoder_parameter = settings['decoder']
        training_parameters = settings['training']

        model_encode = VAE_encode(**encoder_parameter)
        model_decode = VAE_decode(**decoder_parameter)

        model_encode.train()
        model_decode.train()

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        print('*'*15, ': -->', device)

        data = torch.tensor(data, dtype=torch.float).to(device)

        train_valid_test_size=[0.5, 0.5, 0.0]
        x = [i for i in range(len(data))]  # random shuffle input
        shuffle(x)
        data = data[x]
        idx_traintest=int(len(data)*train_valid_test_size[0])
        idx_trainvalid=idx_traintest+int(len(data)*train_valid_test_size[1])
        data_train=data[0:idx_traintest]
        data_valid=data[idx_traintest:idx_trainvalid]
        data_test=data[idx_trainvalid:]

        num_batches_train = int(len(data_train) / batch_size)
        num_batches_valid = int(len(data_valid) / batch_size)

        model_encode = VAE_encode(**encoder_parameter).to(device)
        model_decode = VAE_decode(**decoder_parameter).to(device)
        print("start training")
        train_model(data_train=data_train, data_valid=data_valid, **training_parameters, encoding_alphabet=encoding_alphabet)

        with open('COMPLETED', 'w') as content:
            content.write('exit code: 0')


    except AttributeError:
        _, error_message,_ = sys.exc_info()
        print(error_message)
