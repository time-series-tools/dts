"""
Main script for training and evaluating a TCN on a multi-step forecasting task.
You can chose bewteen:
    - Running a simple experiment
    - Running multiple experiments trying out diffrent combinations of hyperparamters (grid-search)
"""
import warnings
warnings.filterwarnings(action='ignore')

from keras.callbacks import EarlyStopping
from keras.optimizers import Adam

from dts import config
from dts.datasets import uci_single_households
from dts.datasets import gefcom2014
from dts import logger
from dts.utils.plot import plot
from dts.utils import metrics
from dts.utils import run_grid_search, run_single_experiment
from dts.utils import DTSExperiment, log_metrics, get_args
from dts.utils.decorators import f_main
from dts.utils.split import *
from dts.models.TCN import *
import time
import os

args = get_args()


@f_main(args=args)
def main(_run):
    ################################
    # Load Experiment's paramaters #
    ################################
    params = vars(args)
    logger.info(params)

    ################################
    #         Load Dataset         #
    ################################
    dataset_name = params['dataset']
    if dataset_name == 'gefcom':
        dataset = gefcom2014
    else:
        dataset = uci_single_households

    data = dataset.load_data(fill_nan='median',
                             preprocessing=True,
                             split_type='simple',
                             is_train=params['train'],
                             detrend=params['detrend'],
                             exogenous_vars=params['exogenous'],
                             use_prebuilt=True)
    scaler, train, test, trend = data['scaler'], data['train'], data['test'], data['trend']
    if not params['detrend']:
        trend = None

    X_train, y_train = get_rnn_inputs(train,
                                      window_size=params['input_sequence_length'],
                                      horizon=params['output_sequence_length'],
                                      shuffle=True,
                                      multivariate_output=params['exogenous'])

    ################################
    #     Build & Train Model      #
    ################################

    tcn = TCNModel(layers=params['layers'],
                   filters=params['out_channels'],
                   kernel_size=params['kernel_size'],
                   kernel_initializer='glorot_normal',
                   kernel_regularizer=l2(params['l2_reg']),
                   bias_regularizer=l2(params['l2_reg']),
                   dilation_rate=params['dilation'],
                   use_bias=False,
                   return_sequence=True,
                   tcn_type=params['tcn_type'])

    if params['exogenous']:
        exog_var_train = y_train[:, :, 1:]  # [n_samples, horizon, n_features]
        y_train = y_train[:, :, 0]          # [n_samples, horizon]
        conditions_shape = (exog_var_train.shape[1], exog_var_train.shape[-1])

        X_test, y_test = get_rnn_inputs(test,
                                        window_size=params['input_sequence_length'],
                                        horizon=params['output_sequence_length'],
                                        shuffle=False,
                                        multivariate_output=True)
        exog_var_test = y_test[:, :, 1:]  # [n_samples, horizon, n_features]
        y_test = y_test[:, :, 0]          # [n_samples, horizon]
    else:
        X_test, y_test = get_rnn_inputs(test,
                                        window_size=params['input_sequence_length'],
                                        horizon=params['output_sequence_length'],
                                        shuffle=False)
        exog_var_train = None
        exog_var_test = None
        conditions_shape = None

    # IMPORTANT: Remember to pass the trend values through the same ops as the inputs values
    if params['detrend']:
        X_trend_test, y_trend_test = get_rnn_inputs(trend[1],
                                                    window_size=params['input_sequence_length'],
                                                    horizon=params['output_sequence_length'],
                                                    shuffle=False)
        trend = y_trend_test

    model = tcn.build_model(input_shape=(X_train.shape[1], X_train.shape[-1]),
                            horizon=params['output_sequence_length'],
                            conditions_shape=conditions_shape,
                            use_final_dense=True)

    if params['load'] is not None:
        logger.info("Loading model's weights from disk using {}".format(params['load']))
        model.load_weights(params['load'])

    optimizer = Adam(params['learning_rate'])
    model.compile(optimizer=optimizer, loss=['mse'], metrics=metrics)
    callbacks = [EarlyStopping(patience=50, monitor='val_loss')]

    if params['exogenous'] and params['tcn_type'] == 'conditional_tcn':
        history = model.fit([X_train, exog_var_train], y_train,
                            validation_split=0.1,
                            batch_size=params['batch_size'],
                            epochs=params['epochs'],
                            callbacks=callbacks,
                            verbose=2)
    else:
        history = model.fit(X_train, y_train,
                            validation_split=0.1,
                            batch_size=params['batch_size'],
                            epochs=params['epochs'],
                            callbacks=callbacks,
                            verbose=2)

    ################################
    #          Save weights        #
    ################################
    model_filepath = os.path.join(
        config['weights'],'{}_{}_{}'
            .format(params['tcn_type'], params['dataset'], time.time()))
    model.save_weights(model_filepath)
    logger.info("Model's weights saved at {}".format(model_filepath))

    #################################
    # Evaluate on Validation & Test #
    #################################
    fn_inverse_val = lambda x: dataset.inverse_transform(x, scaler=scaler, trend=None)
    fn_inverse_test = lambda x: dataset.inverse_transform(x, scaler=scaler, trend=trend)
    fn_plot = lambda x: plot(x, dataset.SAMPLES_PER_DAY, save_at=None)

    if params['exogenous'] and params['tcn_type'] == 'conditional_tcn':
        val_scores = tcn.evaluate(history.validation_data[:-1], fn_inverse=fn_inverse_val)
        test_scores = tcn.evaluate([[X_test, exog_var_test], y_test], fn_inverse=fn_inverse_test, fn_plot=fn_plot)
    else:
        val_scores = tcn.evaluate(history.validation_data[:-1], fn_inverse=fn_inverse_val)
        test_scores = tcn.evaluate([X_test, y_test], fn_inverse=fn_inverse_test, fn_plot=fn_plot)

    metrics_names = [m.__name__ if not isinstance(m, str) else m for m in model.metrics]
    return dict(zip(metrics_names, val_scores)), \
           dict(zip(metrics_names, test_scores)), \
           model_filepath


if __name__ == '__main__':
    grid_search = args.grid_search
    if grid_search:
        run_grid_search(
            experimentclass=DTSExperiment,
            f_config=args.add_config,
            db_name=config['db'],
            ex_name='tcn_grid_search',
            f_main=main,
            f_metrics=log_metrics,
            observer_type=args.observer)
    else:
        run_single_experiment(
            experimentclass=DTSExperiment,
            db_name=config['db'],
            ex_name='tcn',
            f_main=main,
            f_config=args.add_config,
            f_metrics=log_metrics,
            observer_type=args.observer)