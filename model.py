from keras.models import Model
from keras.layers import (Input,
                          Activation,
                          Dense,
                          Permute,
                          Lambda,
                          merge)
from keras.layers.convolutional import Convolution2D
from keras import backend as K
from theano import tensor as T
from keras.regularizers import l2
import numpy as np
from fcn_plusplus.lib.blocks import (bottleneck,
                                     basic_block,
                                     basic_block_mp,
                                     residual_block)


def _l2(decay):
    """
    Return a new instance of l2 regularizer, or return None
    """
    if decay is not None:
        return l2(decay)
    else:
        return None
    

def _softmax(x):
    """
    Softmax that works on ND inputs.
    """
    e = K.exp(x - K.max(x, axis=-1, keepdims=True))
    s = K.sum(e, axis=-1, keepdims=True)
    return e / s
    
    
def assemble_model(input_shape, num_classes, num_init_blocks, num_main_blocks,
                   main_block_depth, input_num_filters, num_cycles=1,
                   preprocessor_network=None, postprocessor_network=None,
                   mainblock=None, initblock=None, firstblock=None,
                   dropout=0., batch_norm=True, weight_decay=None,
                   bn_kwargs=None, cycles_share_weights=True):
    """
    input_shape : tuple specifiying the 2D image input shape.
    num_classes : number of classes in the segmentation output.
    num_init_blocks : the number of blocks of type initblock, above mainblocks.
        These blocks always have the same number of channels as the first
        convolutional layer in the model.
    num_main_blocks : the number of blocks of type mainblock, below initblocks.
        These blocks double (halve) in number of channels at each downsampling
        (upsampling).
    main_block_depth : an integer or list of integers specifying the number of
        repetitions of each mainblock. A list must contain as many values as
        there are main_blocks in the downward (or upward -- it's mirrored) path
        plus one for the across path.
    input_num_filters : the number channels in the first (last) convolutional
        layer in the model (and of each initblock).
    num_cycles : number of times to cycle the down/up processing pair.
    preprocessor_network : a neural network for preprocessing the input data.
    postprocessor_network : a neural network for postprocessing the data fed
        to the classifier.
    mainblock : a layer defining the mainblock (bottleneck by default).
    initblock : a layer defining the initblock (basic_block_mp by default).
    firstblock : a layer defining the firstblock (basic_block_mp by default).
    use_skip_blocks : pass features skipped along long_skip through skipblocks.
    dropout : the dropout probability, introduced in every block.
    batch_norm : enable or disable batch normalization.
    weight_decay : the weight decay (L2 penalty) used in every convolution.
    cycles_share_weights : share network weights across cycles.
    """
    
    '''
    By default, use depth 2 basic_block for mainblock
    '''
    if mainblock is None:
        mainblock = basic_block
    if initblock is None:
        initblock = basic_block_mp
    if firstblock is None:
        firstblock = basic_block_mp
    
    '''
    main_block_depth can be a list per block or a single value 
    -- ensure the list length is correct (if list) and that no length is 0
    '''
    if not hasattr(main_block_depth, '__len__'):
        if main_block_depth==0:
            raise ValueError("main_block_depth must never be zero")
    else:
        if len(main_block_depth)!=num_main_blocks+1:
            raise ValueError("main_block_depth must have " 
                             "`num_main_blocks+1` values when " 
                             "passed as a list")
        for d in main_block_depth:
            if d==0:
                raise ValueError("main_block_depth must never be zero")
    
    '''
    Returns the depth of a mainblock for a given pooling level.
    '''
    def get_repetitions(level):
        if hasattr(main_block_depth, '__len__'):
            return main_block_depth[level]
        return main_block_depth
    
    '''
    Merge tensors, changing the number of feature maps in the first input
    to match that of the second input. Feature maps in the first input are
    reweighted.
    
    If weight sharing is enabled, reuse old convolutions.
    '''
    def merge_into(x, into, skips, cycle, direction, depth, bn_status):
        if x._keras_shape[1] != into._keras_shape[1]:
            if cycles_share_weights and depth in skips[cycle-1][direction]:
                conv_layer = skips[cycle-1][direction][depth]
            else:
                conv_layer = Convolution2D(into._keras_shape[1], 1, 1,
                                           init='he_normal',
                                           border_mode='valid',
                                           W_regularizer=_l2(weight_decay))
            skips[cycle][direction][depth] = conv_layer
            x = conv_layer(x)
        
        out = merge([x, into], mode='sum')
        if bn_status==False:
            # When batch norm is disabled, halve the merged values since it is
            # not a residual that is being summed in on a long skip connection.
            out = Lambda(lambda x: x/2., output_shape=lambda x:x)(out)
        return out
        #return x
    
    '''
    Given some block function and an input tensor, return a reusable model
    instantiating that block function. This is to allow weight sharing.
    '''
    def make_block(block_func, x, cycle=0):
        x_nb_filter = x._keras_shape[1]
        input = Input(shape=(x_nb_filter, None, None))
        model = Model(input, block_func(input))
        return model
    
    '''
    Constant kwargs passed to the init and main blocks.
    '''
    block_kwargs = {'dropout': dropout,
                    'weight_decay': weight_decay,
                    'bn_kwargs': bn_kwargs}
    
    # INPUT
    input = Input(shape=input_shape)
    
    # Preprocessing
    if preprocessor_network is not None:
        input = preprocessor_network(input)
    
    '''
    Build the blocks for all cycles, contracting and expanding in each cycle.
    '''
    tensors = [] # feature tensors
    blocks = []  # residual block layers
    skips = []   # 1x1 kernel convolution layers on long skip connections
    x = input
    for cycle in range(num_cycles):
        # Create tensors and layer lists for this cycle.
        tensors.append({'down': {}, 'up': {}, 'across': {}})
        blocks.append({'down': {}, 'up': {}, 'across': {}})
        skips.append({'down': {}, 'up': {}, 'across': {}})
        
        # On the down path, batch norm is only used for the first down.
        bn_down = batch_norm if cycle==0 else False
        
        # First block
        if cycle > 0:
            x = merge_into(x, tensors[cycle-1]['up'][0], skips=skips,
                           cycle=cycle, direction='down', depth=0,
                           bn_status=bn_down)
        if cycles_share_weights and cycle > 1:
            block = blocks[cycle-1]['down'][0]
        else:
            block_func = firstblock(nb_filter=input_num_filters,
                                    subsample=False,
                                    upsample=False,
                                    skip=False,
                                    batch_norm=bn_down,
                                    **block_kwargs)
            block = make_block(block_func, x, cycle)
        x = block(x)
        blocks[cycle]['down'][0] = block
        tensors[cycle]['down'][0] = x
        print("Cycle {} - FIRST DOWN: {}".format(cycle, x._keras_shape))
        
        # DOWN (initial subsampling blocks)
        for b in range(num_init_blocks):
            depth = b+1
            if cycle > 0:
                x = merge_into(x, tensors[cycle-1]['up'][depth], skips=skips,
                               cycle=cycle, direction='down', depth=depth,
                               bn_status=bn_down)
            if cycles_share_weights and cycle > 1:
                block = blocks[cycle-1]['down'][depth]
            else:
                block_func = residual_block(initblock,
                                            nb_filter=input_num_filters,
                                            repetitions=1,
                                            skip=True,
                                            subsample=True,
                                            upsample=False,
                                            batch_norm=bn_down,
                                            **block_kwargs)
                block = make_block(block_func, x, cycle)
            x = block(x)
            blocks[cycle]['down'][depth] = block
            tensors[cycle]['down'][depth] = x
            print("Cycle {} - INIT DOWN {}: {}".format(cycle, b,
                                                       x._keras_shape))
        
        # DOWN (resnet blocks)
        for b in range(num_main_blocks):
            depth = b+1+num_init_blocks
            if cycle > 0:
                x = merge_into(x, tensors[cycle-1]['up'][depth], skips=skips,
                               cycle=cycle, direction='down', depth=depth,
                               bn_status=bn_down)
            if cycles_share_weights and cycle > 1:
                block = blocks[cycle-1]['down'][depth]
            else:
                block_func = residual_block(mainblock,
                                            nb_filter=input_num_filters*(2**b),
                                            repetitions=get_repetitions(b),
                                            skip=True,
                                            subsample=True,
                                            upsample=False,
                                            batch_norm=bn_down,
                                            **block_kwargs)
                block = make_block(block_func, x, cycle)
            x = block(x)
            blocks[cycle]['down'][depth] = block
            tensors[cycle]['down'][depth] = x
            print("Cycle {} - MAIN DOWN {}: {}".format(cycle, b,
                                                       x._keras_shape))
            
        # ACROSS
        if cycle > 0:
            x = merge_into(x, tensors[cycle-1]['across'][0], skips=skips,
                           cycle=cycle, direction='across', depth=0,
                           bn_status=bn_down)
        if cycles_share_weights and cycle > 1:
            block = blocks[cycle-1]['across'][0]
        else:
            block_func = residual_block( \
                                  mainblock,
                                  nb_filter=input_num_filters*(2**b),
                                  repetitions=get_repetitions(num_main_blocks),
                                  skip=True,
                                  subsample=True,
                                  upsample=True,
                                  batch_norm=bn_down,
                                  **block_kwargs)
            block = make_block(block_func, x, cycle)
        x = block(x)
        blocks[cycle]['across'][0] = block
        tensors[cycle]['across'][0] = x
        print("Cycle {} - ACROSS: {}".format(cycle, x._keras_shape))

        # On the up path, batch norm only in the last cycle.
        bn_up = batch_norm if cycle==num_cycles-1 else False

        # UP (resnet blocks)
        for b in range(num_main_blocks-1, -1, -1):
            depth = b+1+num_init_blocks
            x = merge_into(x, tensors[cycle]['down'][depth], skips=skips,
                           cycle=cycle, direction='up', depth=depth,
                           bn_status=bn_up)
            if cycles_share_weights and cycle > 0 and cycle < num_cycles-1:
                block = blocks[cycle-1]['up'][depth]
            else:
                
                block_func = residual_block(mainblock,
                                            nb_filter=input_num_filters*(2**b),
                                            repetitions=get_repetitions(b),
                                            skip=True,
                                            subsample=False,
                                            upsample=True,
                                            batch_norm=bn_up,
                                            **block_kwargs)
                block = make_block(block_func, x, cycle)
            x = block(x)
            blocks[cycle]['up'][depth] = block
            tensors[cycle]['up'][depth] = x
            print("Cycle {} - MAIN UP {}: {}".format(cycle, b,
                                                     x._keras_shape))
        
        # UP (final upsampling blocks)
        for b in range(num_init_blocks-1, -1, -1):
            depth = b+1
            x = merge_into(x, tensors[cycle]['down'][depth], skips=skips,
                           cycle=cycle, direction='up', depth=depth,
                           bn_status=bn_up)
            if cycles_share_weights and cycle > 0 and cycle < num_cycles-1:
                block = blocks[cycle-1]['up'][depth]
            else:
                block_func = residual_block(initblock,
                                            nb_filter=input_num_filters,
                                            repetitions=1,
                                            skip=True,
                                            subsample=False,
                                            upsample=True,
                                            batch_norm=bn_up,
                                            **block_kwargs)
                block = make_block(block_func, x, cycle)
            x = block(x)
            blocks[cycle]['up'][depth] = block
            tensors[cycle]['up'][depth] = x
            print("Cycle {} - INIT UP {}: {}".format(cycle, b,
                                                     x._keras_shape))
            
        # Final block
        x = merge_into(x, tensors[cycle]['down'][0], skips=skips,
                       cycle=cycle, direction='up', depth=0,
                       bn_status=bn_up)
        if cycles_share_weights and cycle > 0 and cycle < num_cycles-1:
            block = blocks[cycle-1]['up'][0]
        else:
            block_func = firstblock(nb_filter=input_num_filters,
                                    subsample=False,
                                    upsample=False,
                                    skip=False,
                                    batch_norm=bn_up,
                                    **block_kwargs)
            block = make_block(block_func, x, cycle)
        x = block(x)
        blocks[cycle]['up'][0] = block
        tensors[cycle]['up'][0] = x
        if cycle > 0:
            # Merge preclassifier outputs across all cycles.
            x = merge_into(x, tensors[cycle-1]['up'][0], skips=skips,
                           cycle=cycle, direction='up', depth=-1,
                           bn_status=bn_up)
        print("Cycle {} - FIRST UP: {}".format(cycle, x._keras_shape))
            
    # Postprocessing
    if postprocessor_network is not None:
        x = postprocessor_network(x)
    
    # OUTPUT (SOFTMAX)
    if num_classes is not None:
        # Linear classifier
        output = Convolution2D(num_classes,1,1,activation='linear', 
                               W_regularizer=_l2(weight_decay))(x)
        output = Permute((2,3,1))(output)
        if num_classes==1:
            output = Activation('sigmoid')(output)
        else:
            output = Activation(_softmax)(output)
        output = Permute((3,1,2))(output)
    else:
        # No classifier
        output = x
    
    # MODEL
    model = Model(input=input, output=output)

    return model
